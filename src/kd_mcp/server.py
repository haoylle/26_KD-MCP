from __future__ import annotations

import csv
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ValidationError

APP_NAME = "kd-mcp"
mcp = FastMCP(APP_NAME)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class KdConfig(BaseModel):
    kd_exe: str
    symbol_path: str | None = None
    default_transport: str = "net"
    default_port: int = 50000
    default_key: str
    default_target: str | None = None
    default_kdnet: str | None = None
    state_file: str = r"C:\mcp-state\kd-session.json"
    startup_timeout_sec: int = 30
    command_timeout_sec: int = 60
    output_tail_chars: int = 200000
    auto_initial_break: bool = False
    continue_wait_sec: float = 5.0
    server_port: int = 50055
    remote_server: str = "127.0.0.1"
    server_state_file: str = r"C:\mcp-state\kd-server.json"
    workdir: str = r"C:\mcp-state\kd-work"
    server_startup_wait_sec: float = 1.0


class SecurityConfig(BaseModel):
    allowed_command_prefixes: list[str] = Field(default_factory=list)
    denied_patterns: list[str] = Field(default_factory=list)


class Config(BaseModel):
    kd: KdConfig
    security: SecurityConfig = Field(default_factory=SecurityConfig)


@dataclass
class KdSession:
    id: str
    process: subprocess.Popen
    output: list[str] = field(default_factory=list)
    q: queue.Queue[str] = field(default_factory=queue.Queue)
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    command_line: list[str] = field(default_factory=list)
    command_lock: threading.Lock = field(default_factory=threading.Lock)


_sessions: dict[str, KdSession] = {}
_config_cache: Config | None = None


def _load_config() -> Config:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    cfg_path = os.environ.get("KD_MCP_CONFIG") or "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise RuntimeError(f"Invalid config file {cfg_path}: {e}") from e
    _config_cache = cfg
    return cfg


def _check_command_allowed(command: str) -> None:
    cfg = _load_config()
    low = command.lower().strip()
    for pat in cfg.security.denied_patterns:
        if pat.lower() in low:
            raise RuntimeError(f"KD command denied by policy: contains pattern {pat!r}")
    prefixes = cfg.security.allowed_command_prefixes
    if prefixes and not any(low.startswith(p.lower()) for p in prefixes):
        raise RuntimeError("KD command denied by policy: not in allowed_command_prefixes")


def _reader(sess: KdSession) -> None:
    assert sess.process.stdout is not None
    while True:
        chunk = sess.process.stdout.read(1)
        if chunk == "" and sess.process.poll() is not None:
            break
        if chunk:
            sess.output.append(chunk)
            sess.q.put(chunk)
            limit = _load_config().kd.output_tail_chars
            joined_len = sum(len(x) for x in sess.output)
            while joined_len > limit and len(sess.output) > 1:
                joined_len -= len(sess.output.pop(0))


def _drain_queue(sess: KdSession) -> None:
    while True:
        try:
            sess.q.get_nowait()
        except queue.Empty:
            return


def _wait_collect(sess: KdSession, timeout: float, prompt_hint: bool = True) -> tuple[str, bool]:
    deadline = time.time() + timeout
    chunks: list[str] = []
    prompt_seen = False
    while time.time() < deadline:
        try:
            item = sess.q.get(timeout=0.2)
            chunks.append(item)
            text = "".join(chunks)
            if prompt_hint and re.search(r"(?:^|[\r\n])kd>\s*$", text, re.IGNORECASE):
                prompt_seen = True
                break
        except queue.Empty:
            if sess.process.poll() is not None:
                break
    return "".join(chunks), prompt_seen


def _build_cmd(port: int, key: str, host: str | None = None) -> list[str]:
    cfg = _load_config().kd
    kd_exe = cfg.kd_exe
    if not Path(kd_exe).exists():
        raise RuntimeError(f"kd.exe not found: {kd_exe}")
    transport = _build_kdnet_transport(port, key, host)
    cmd = [kd_exe]
    if cfg.symbol_path:
        cmd += ["-y", cfg.symbol_path]
    cmd += ["-k", transport]
    return cmd


def _decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-16-le", "cp949", "cp932", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _server_state_path() -> Path:
    return Path(_load_config().kd.server_state_file)


def _load_server_state() -> dict[str, Any]:
    path = _server_state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_server_state(state: dict[str, Any]) -> None:
    path = _server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _build_kdnet_transport(port: int, key: str, host: str | None = None) -> str:
    transport = f"net:port={port},key={key}"
    if host:
        transport += f",target={host}"
    return transport


def _build_remote_spec(remote_server: str | None = None, server_port: int | None = None, remote_spec: str | None = None) -> str:
    if remote_spec:
        return remote_spec
    cfg = _load_config().kd
    return f"tcp:server={remote_server or cfg.remote_server},port={int(server_port or cfg.server_port)}"


def _is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        cp = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=False,
            timeout=5,
        )
        out = _decode_bytes(cp.stdout)
        return str(pid) in out and "INFO:" not in out
    except Exception:
        return False


def _kill_pid(pid: int, force: bool = True) -> tuple[bool, str]:
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    cp = subprocess.run(args, capture_output=True, text=False, timeout=15)
    output = (_decode_bytes(cp.stdout) + _decode_bytes(cp.stderr)).strip()
    return cp.returncode == 0, output


def _tail_file(path: Path, max_bytes: int = 65536) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        return _decode_bytes(f.read())


def _list_processes_by_names(names: list[str]) -> list[dict[str, Any]]:
    targets = {name.lower() for name in names}
    try:
        cp = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=False, timeout=10)
        rows = list(csv.reader(_decode_bytes(cp.stdout).splitlines()))
    except Exception as exc:
        return [{"error": str(exc)}]

    results: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 2:
            continue
        image = row[0].strip()
        pid_text = row[1].strip()
        if image.lower() not in targets:
            continue
        item: dict[str, Any] = {"image_name": image}
        if pid_text.isdigit():
            item["pid"] = int(pid_text)
        if len(row) > 3:
            item["session_name"] = row[2]
            item["session_num"] = row[3]
        if len(row) > 4:
            item["mem_usage"] = row[4]
        results.append(item)
    return results


def _reader_thread_bytes(stream: Any, outq: queue.Queue[bytes]) -> None:
    try:
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            outq.put(chunk)
    except Exception as exc:
        outq.put(f"\n[reader-error] {exc}\n".encode("utf-8", errors="replace"))


def _send_ctrl_break_to_console_process(pid: int) -> tuple[bool, str]:
    helper = (
        "import ctypes,sys,time;"
        "pid=int(sys.argv[1]);"
        "k=ctypes.WinDLL('kernel32',use_last_error=True);"
        "k.FreeConsole();"
        "\nif not k.AttachConsole(pid):\n"
        "    print('AttachConsole failed %d' % ctypes.get_last_error()); sys.exit(2)\n"
        "k.SetConsoleCtrlHandler(None, True);"
        "\ntry:\n"
        "    ok=k.GenerateConsoleCtrlEvent(1,0);"
        "    print('GenerateConsoleCtrlEvent ok=%s err=%d' % (bool(ok), ctypes.get_last_error()));"
        "    time.sleep(0.2);"
        "    sys.exit(0 if ok else 3)\n"
        "finally:\n"
        "    k.FreeConsole(); k.SetConsoleCtrlHandler(None, False)\n"
    )
    cp = subprocess.run(
        [sys.executable, "-c", helper, str(pid)],
        capture_output=True,
        text=False,
        timeout=10,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )
    output = (_decode_bytes(cp.stdout) + _decode_bytes(cp.stderr)).strip()
    if cp.returncode in (0, 0xC000013A, -1073741510):
        return True, output or f"CTRL_BREAK_EVENT sent; helper exit code {cp.returncode}"
    return False, output or f"helper exit code {cp.returncode}"


def _start_process(cmd: list[str]) -> KdSession:
    sid = uuid.uuid4().hex
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
    )
    sess = KdSession(id=sid, process=p, command_line=cmd)
    _sessions[sid] = sess
    t = threading.Thread(target=_reader, args=(sess,), daemon=True)
    t.start()
    return sess


def _session(session_id: str) -> KdSession:
    if session_id not in _sessions:
        raise RuntimeError("Unknown KD session_id")
    sess = _sessions[session_id]
    sess.last_used = time.time()
    return sess


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Verify kd-mcp config and kd.exe path."""
    cfg = _load_config().kd
    server_state = _load_server_state()
    return {
        "ok": Path(cfg.kd_exe).exists(),
        "kd_exe": cfg.kd_exe,
        "symbol_path": cfg.symbol_path,
        "default_target": cfg.default_target,
        "default_kdnet": cfg.default_kdnet,
        "state_file": cfg.state_file,
        "continue_wait_sec": cfg.continue_wait_sec,
        "server_state_file": cfg.server_state_file,
        "server_port": cfg.server_port,
        "remote_server": cfg.remote_server,
        "tracked_server_pid": server_state.get("server_pid"),
        "tracked_server_running": _is_pid_running(server_state.get("server_pid")),
        "active_sessions": list(_sessions.keys()),
    }


@mcp.tool()
def start_kd(
    port: int | None = None,
    key: str | None = None,
    target: str | None = None,
    kdnet: str | None = None,
) -> dict[str, Any]:
    """Start kd.exe for KDNET using explicit port/key values."""
    cfg = _load_config().kd
    active_port = port or cfg.default_port
    active_key = key or cfg.default_key
    active_target = target or cfg.default_target
    active_kdnet = kdnet or cfg.default_kdnet
    if active_kdnet:
        if not Path(cfg.kd_exe).exists():
            raise RuntimeError(f"kd.exe not found: {cfg.kd_exe}")
        cmd = [cfg.kd_exe]
        if cfg.symbol_path:
            cmd += ["-y", cfg.symbol_path]
        cmd += ["-k", active_kdnet]
    else:
        cmd = _build_cmd(active_port, active_key, active_target)
    sess = _start_process(cmd)
    output, prompt_seen = _wait_collect(sess, cfg.startup_timeout_sec, prompt_hint=True)
    if cfg.auto_initial_break:
        try:
            break_in(sess.id)
        except Exception:
            pass
    return {
        "session_id": sess.id,
        "pid": sess.process.pid,
        "command_line": cmd,
        "initial_output": output,
        "prompt_seen": prompt_seen,
        "target": active_target,
        "kdnet": active_kdnet or _build_kdnet_transport(active_port, active_key, active_target),
    }


@mcp.tool()
def start_from_state(state_file: str | None = None) -> dict[str, Any]:
    """Start kd.exe using the shared state file written by winrm-mcp configure_kdnet."""
    cfg = _load_config().kd
    path = Path(state_file or cfg.state_file)
    if not path.is_file():
        raise RuntimeError(f"State file not found: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != "winrm-kd-session-v1":
        raise RuntimeError("Unsupported state file schema")
    res = start_kd(port=int(state["port"]), key=str(state["key"]), target=state.get("guest_host"))
    res["state_file"] = str(path)
    res["state"] = {k: v for k, v in state.items() if k != "key"}
    return res


@mcp.tool()
def kd_command(session_id: str, command: str, timeout_sec: int | None = None) -> dict[str, Any]:
    """Send a command to a running kd.exe session and collect output."""
    _check_command_allowed(command)
    sess = _session(session_id)
    with sess.command_lock:
        if sess.process.poll() is not None:
            raise RuntimeError(f"KD process exited with code {sess.process.returncode}")
        _drain_queue(sess)
        assert sess.process.stdin is not None
        sess.process.stdin.write(command.rstrip("\n") + "\n")
        sess.process.stdin.flush()
        output, prompt_seen = _wait_collect(sess, timeout_sec or _load_config().kd.command_timeout_sec)
    return {
        "ok": True,
        "session_id": session_id,
        "command": command,
        "output": output,
        "prompt_seen": prompt_seen,
        "process_alive": sess.process.poll() is None,
    }


@mcp.tool()
def break_in(session_id: str) -> dict[str, Any]:
    """Send a break signal to kd.exe. On Windows this uses CTRL_BREAK_EVENT when possible."""
    sess = _session(session_id)
    with sess.command_lock:
        if sess.process.poll() is not None:
            raise RuntimeError("KD process is not running")
        _drain_queue(sess)
        if os.name == "nt":
            sess.process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            sess.process.send_signal(signal.SIGINT)
        output, prompt_seen = _wait_collect(sess, 15, prompt_hint=True)
    return {"ok": True, "session_id": session_id, "output": output, "prompt_seen": prompt_seen}


@mcp.tool()
def continue_go(session_id: str, wait_sec: float | None = None) -> dict[str, Any]:
    """Send the KD 'g' command to continue target execution, then optionally wait before returning."""
    cfg = _load_config().kd
    wait = cfg.continue_wait_sec if wait_sec is None else wait_sec
    res = kd_command(session_id, "g", timeout_sec=5)
    if wait and wait > 0:
        time.sleep(wait)
    res["waited_sec"] = wait
    res["note"] = "Target continued. WinRM can be retried after the wait if the guest network stack is running."
    return res


@mcp.tool()
def resume_for_winrm(session_id: str, wait_sec: float | None = None) -> dict[str, Any]:
    """Continue from a KD break/bp state and wait so WinRM has time to respond again."""
    cfg = _load_config().kd
    wait = cfg.continue_wait_sec if wait_sec is None else wait_sec
    res = continue_go(session_id, wait_sec=wait)
    res["winrm_retry_hint"] = "KD break state pauses the guest kernel and network stack. Retry WinRM after this wait."
    return res


@mcp.tool()
def read_output(session_id: str, tail_chars: int | None = None) -> dict[str, Any]:
    """Return buffered kd.exe output for a session."""
    sess = _session(session_id)
    text = "".join(sess.output)
    if tail_chars:
        text = text[-tail_chars:]
    return {"session_id": session_id, "output": text, "process_alive": sess.process.poll() is None, "returncode": sess.process.poll()}


@mcp.tool()
def stop_kd(session_id: str, terminate_target: bool = False) -> dict[str, Any]:
    """Stop kd.exe. By default only the debugger process is stopped, not the target."""
    sess = _session(session_id)
    if sess.process.poll() is None:
        try:
            if terminate_target:
                kd_command(session_id, "q", timeout_sec=3)
            else:
                sess.process.terminate()
        except Exception:
            sess.process.kill()
    try:
        sess.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        sess.process.kill()
    _sessions.pop(session_id, None)
    return {"ok": True, "session_id": session_id, "returncode": sess.process.returncode}


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """List active KD sessions."""
    return {
        "sessions": [
            {
                "session_id": sid,
                "pid": s.process.pid,
                "alive": s.process.poll() is None,
                "started_at_unix": s.started_at,
                "last_used_unix": s.last_used,
                "command_line": s.command_line,
            }
            for sid, s in _sessions.items()
        ]
    }


@mcp.tool()
def start_kd_server(
    kdnet: str | None = None,
    port: int | None = None,
    key: str | None = None,
    target: str | None = None,
    server_port: int | None = None,
    remote_server: str | None = None,
    symbol_path: str | None = None,
    hidden: bool = True,
    noio: bool = True,
    break_on_connect: bool = False,
    restart_existing: bool = False,
    startup_wait_sec: float | None = None,
    initial_commands: str | list[str] | None = None,
    log_path: str | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Start a hidden kd.exe owner process with debugger remoting enabled."""
    cfg = _load_config().kd
    state = _load_server_state()
    old_pid = state.get("server_pid")
    if old_pid and _is_pid_running(int(old_pid)):
        if not restart_existing:
            return {
                "status": "already_running",
                "server_pid": old_pid,
                "state": state,
                "hint": "Pass restart_existing=true to replace the tracked kd server.",
            }
        _kill_pid(int(old_pid), force=True)
        time.sleep(0.5)

    kd_path = cfg.kd_exe
    if not Path(kd_path).exists():
        raise RuntimeError(f"kd.exe not found: {kd_path}")

    active_target = target or cfg.default_target
    kdnet_value = kdnet or cfg.default_kdnet or _build_kdnet_transport(port or cfg.default_port, key or cfg.default_key, active_target)
    active_workdir = Path(workdir or cfg.workdir)
    active_workdir.mkdir(parents=True, exist_ok=True)
    logs_dir = active_workdir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = active_workdir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    active_server_port = int(server_port or cfg.server_port)
    active_remote_server = remote_server or cfg.remote_server
    server_transport = f"tcp:port={active_server_port}"
    remote_spec = _build_remote_spec(active_remote_server, active_server_port, None)
    active_log_path = Path(log_path) if log_path else logs_dir / f"kd_server_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log"
    active_log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [kd_path, "-server", server_transport]
    if noio:
        cmd.append("-noio")
    if symbol_path or cfg.symbol_path:
        cmd.extend(["-y", symbol_path or cfg.symbol_path or ""])
    if break_on_connect:
        cmd.append("-b")
    if initial_commands:
        init = initial_commands if isinstance(initial_commands, str) else "; ".join(str(x) for x in initial_commands)
        cmd.extend(["-c", init])
    cmd.extend(["-k", kdnet_value, "-logo", str(active_log_path)])

    creationflags = CREATE_NEW_PROCESS_GROUP
    creationflags |= CREATE_NO_WINDOW if hidden else CREATE_NEW_CONSOLE
    proc = subprocess.Popen(
        cmd,
        cwd=str(active_workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    new_state = {
        "server_pid": proc.pid,
        "kd_path": kd_path,
        "kdnet": kdnet_value,
        "server_port": active_server_port,
        "remote_server": active_remote_server,
        "remote_spec": remote_spec,
        "workdir": str(active_workdir),
        "log_path": str(active_log_path),
        "hidden": hidden,
        "noio": noio,
        "started_at_unix": time.time(),
        "command_line": cmd,
    }
    _save_server_state(new_state)
    time.sleep(startup_wait_sec if startup_wait_sec is not None else cfg.server_startup_wait_sec)
    return {
        "status": "started" if _is_pid_running(proc.pid) else "exited_early",
        "server_pid": proc.pid,
        "remote_spec": remote_spec,
        "log_path": str(active_log_path),
        "command_line": cmd,
        "log_tail": _tail_file(active_log_path, 8192),
        "target": active_target,
        "kdnet": kdnet_value,
        "warning": "Do not attach this hidden server and a direct KD session to the same KDNET target at the same time.",
    }


@mcp.tool()
def start_kd_server_from_state(
    state_file: str | None = None,
    server_port: int | None = None,
    remote_server: str | None = None,
    restart_existing: bool = False,
    hidden: bool = True,
    noio: bool = True,
) -> dict[str, Any]:
    """Start the hidden kd.exe remoting server using the shared WinRM state file."""
    cfg = _load_config().kd
    path = Path(state_file or cfg.state_file)
    if not path.is_file():
        raise RuntimeError(f"State file not found: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != "winrm-kd-session-v1":
        raise RuntimeError("Unsupported state file schema")
    res = start_kd_server(
        port=int(state["port"]),
        key=str(state["key"]),
        target=state.get("guest_host"),
        server_port=server_port,
        remote_server=remote_server,
        restart_existing=restart_existing,
        hidden=hidden,
        noio=noio,
    )
    res["state_file"] = str(path)
    res["state"] = {k: v for k, v in state.items() if k != "key"}
    return res


@mcp.tool()
def kd_server_status() -> dict[str, Any]:
    """Return tracked hidden kd.exe server state and local process status."""
    state = _load_server_state()
    status = dict(state)
    pid = state.get("server_pid")
    status["server_running"] = _is_pid_running(int(pid)) if pid else False
    status["server_state_file"] = str(_server_state_path())
    status["known_kd_processes"] = _list_processes_by_names(["kd.exe", "windbg.exe"])
    if state.get("log_path"):
        status["log_tail"] = _tail_file(Path(state["log_path"]), 8192)
    return status


@mcp.tool()
def stop_kd_server(pid: int | None = None, force: bool = True) -> dict[str, Any]:
    """Stop the tracked hidden kd.exe server process."""
    state = _load_server_state()
    active_pid = int(pid or state.get("server_pid") or 0)
    if not active_pid:
        return {"status": "no_tracked_server"}
    if not _is_pid_running(active_pid):
        return {"status": "not_running", "server_pid": active_pid}
    ok, output = _kill_pid(active_pid, force=force)
    if ok and state.get("server_pid") == active_pid:
        state["server_pid"] = None
        _save_server_state(state)
    return {"status": "stopped" if ok else "failed", "server_pid": active_pid, "output": output}


@mcp.tool()
def kd_server_command(
    command: str,
    timeout_sec: float | None = None,
    remote_spec: str | None = None,
    remote_server: str | None = None,
    server_port: int | None = None,
    keep_client_on_timeout: bool = False,
    append_quit: bool = False,
) -> dict[str, Any]:
    """Run a debugger command through a disposable kd.exe -remote client."""
    state = _load_server_state()
    kd_path = _load_config().kd.kd_exe
    if not Path(kd_path).exists():
        raise RuntimeError(f"kd.exe not found: {kd_path}")
    active_remote_spec = _build_remote_spec(
        remote_server or state.get("remote_server"),
        server_port or state.get("server_port"),
        remote_spec or state.get("remote_spec"),
    )
    timeout = timeout_sec if timeout_sec is not None else _load_config().kd.command_timeout_sec
    marker = "KD_MCP_" + uuid.uuid4().hex
    begin = f"__{marker}_BEGIN__"
    done = f"__{marker}_DONE__"
    script = f".prefer_dml 0\n.echo {begin}\n{command}\n.echo {done}\n"
    if append_quit:
        script += "q\n"

    proc = subprocess.Popen(
        [kd_path, "-remote", active_remote_spec],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    outq: queue.Queue[bytes] = queue.Queue()
    reader = threading.Thread(target=_reader_thread_bytes, args=(proc.stdout, outq), daemon=True)
    reader.start()
    proc.stdin.write(script.encode("utf-8"))
    proc.stdin.flush()

    deadline = time.monotonic() + timeout
    raw = bytearray()
    found_begin = False
    found_done = False
    timed_out = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            chunk = outq.get(timeout=min(0.2, remaining))
            raw.extend(chunk)
            text = _decode_bytes(bytes(raw))
            found_begin = found_begin or begin in text
            if done in text:
                found_done = True
                break
        except queue.Empty:
            if proc.poll() is not None:
                break

    if keep_client_on_timeout and timed_out:
        client_status = "left_running"
    else:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        client_status = "terminated"

    try:
        while True:
            raw.extend(outq.get_nowait())
    except queue.Empty:
        pass

    return {
        "status": "ok" if found_done else ("timeout" if timed_out else "ended_without_marker"),
        "remote_spec": active_remote_spec,
        "client_pid": proc.pid,
        "client_status": client_status,
        "exit_code": proc.poll(),
        "found_begin": found_begin,
        "found_done": found_done,
        "timed_out": timed_out,
        "timeout_sec": timeout,
        "command": command,
        "output": _decode_bytes(bytes(raw)),
        "warning": "Commands like 'g' can naturally run until the next break and may time out when executed through disposable remote clients.",
    }


@mcp.tool()
def kd_server_script(
    script_text: str | None = None,
    script_path: str | None = None,
    timeout_sec: float | None = None,
    remote_spec: str | None = None,
    remote_server: str | None = None,
    server_port: int | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Run a WinDbg script through the hidden remoting server."""
    state = _load_server_state()
    active_workdir = Path(workdir or state.get("workdir") or _load_config().kd.workdir)
    scripts_dir = active_workdir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    if script_text:
        active_script_path = scripts_dir / f"kd_mcp_script_{time.strftime('%Y-%m-%d_%H-%M-%S')}_{uuid.uuid4().hex[:8]}.txt"
        active_script_path.write_text(script_text, encoding="utf-8")
    elif script_path:
        active_script_path = Path(script_path)
    else:
        raise RuntimeError("script_text or script_path is required")
    res = kd_server_command(
        command=f"$$><{active_script_path}",
        timeout_sec=timeout_sec,
        remote_spec=remote_spec,
        remote_server=remote_server,
        server_port=server_port,
    )
    res["script_path"] = str(active_script_path)
    return res


@mcp.tool()
def read_kd_server_log(log_path: str | None = None, max_bytes: int = 65536) -> dict[str, Any]:
    """Tail the hidden kd.exe server log."""
    state = _load_server_state()
    active_log_value = log_path or state.get("log_path")
    if not active_log_value:
        raise RuntimeError("No log_path supplied and no tracked server log exists.")
    active_log_path = Path(active_log_value)
    return {"log_path": str(active_log_path), "max_bytes": max_bytes, "text": _tail_file(active_log_path, max_bytes)}


@mcp.tool()
def open_remote_client(
    remote_spec: str | None = None,
    remote_server: str | None = None,
    server_port: int | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Open a visible kd.exe -remote client without owning the KDNET session."""
    state = _load_server_state()
    kd_path = _load_config().kd.kd_exe
    active_remote_spec = _build_remote_spec(
        remote_server or state.get("remote_server"),
        server_port or state.get("server_port"),
        remote_spec or state.get("remote_spec"),
    )
    active_workdir = Path(workdir or state.get("workdir") or _load_config().kd.workdir)
    active_workdir.mkdir(parents=True, exist_ok=True)
    cmd = [kd_path, "-remote", active_remote_spec]
    proc = subprocess.Popen(cmd, cwd=str(active_workdir), creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP)
    state["last_remote_client_pid"] = proc.pid
    state["last_remote_client_started_at_unix"] = time.time()
    _save_server_state(state)
    return {"status": "opened", "client_pid": proc.pid, "remote_spec": active_remote_spec, "command_line": cmd}


@mcp.tool()
def break_remote_client(
    client_pid: int | None = None,
    open_client_if_needed: bool = True,
    connect_wait_sec: float = 1.0,
    remote_spec: str | None = None,
    remote_server: str | None = None,
    server_port: int | None = None,
) -> dict[str, Any]:
    """Send Ctrl+Break through a visible remote kd client. The hidden server remains the owner."""
    state = _load_server_state()
    active_pid = client_pid or state.get("last_remote_client_pid")
    opened = None
    if not active_pid or not _is_pid_running(int(active_pid)):
        if open_client_if_needed:
            opened = open_remote_client(remote_spec=remote_spec, remote_server=remote_server, server_port=server_port)
            active_pid = opened["client_pid"]
            time.sleep(connect_wait_sec)
        else:
            raise RuntimeError("No running remote client is available for Ctrl+Break.")
    ok, message = _send_ctrl_break_to_console_process(int(active_pid))
    return {
        "status": "sent" if ok else "failed",
        "client_pid": int(active_pid),
        "opened_client": opened,
        "message": message,
        "note": "This sends Ctrl+Break to a visible remote kd client. The hidden kd server remains the session owner.",
    }


@mcp.tool()
def list_kd_processes() -> dict[str, Any]:
    """List local kd.exe and WinDbg processes."""
    return {"processes": _list_processes_by_names(["kd.exe", "windbg.exe"])}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

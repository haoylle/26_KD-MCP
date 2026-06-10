from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
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


class KdConfig(BaseModel):
    kd_exe: str
    symbol_path: str | None = None
    default_transport: str = "net"
    default_port: int = 50000
    default_key: str
    state_file: str = r"C:\mcp-state\kd-session.json"
    startup_timeout_sec: int = 30
    command_timeout_sec: int = 60
    output_tail_chars: int = 200000
    auto_initial_break: bool = False


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
        chunk = sess.process.stdout.readline()
        if chunk == "" and sess.process.poll() is not None:
            break
        if chunk:
            sess.output.append(chunk)
            sess.q.put(chunk)
            limit = _load_config().kd.output_tail_chars
            joined_len = sum(len(x) for x in sess.output)
            while joined_len > limit and len(sess.output) > 1:
                removed = sess.output.pop(0)
                joined_len -= len(removed)


def _wait_collect(sess: KdSession, timeout: float, prompt_hint: bool = True) -> str:
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        try:
            item = sess.q.get(timeout=0.2)
            chunks.append(item)
            text = "".join(chunks)
            if prompt_hint and re.search(r"\n?kd>\s*$", text):
                break
        except queue.Empty:
            if sess.process.poll() is not None:
                break
    return "".join(chunks)


def _build_cmd(port: int, key: str, host: str | None = None) -> list[str]:
    cfg = _load_config().kd
    kd_exe = cfg.kd_exe
    if not Path(kd_exe).exists():
        raise RuntimeError(f"kd.exe not found: {kd_exe}")
    transport = f"net:port={port},key={key}"
    if host:
        transport += f",target={host}"
    cmd = [kd_exe]
    if cfg.symbol_path:
        cmd += ["-y", cfg.symbol_path]
    cmd += ["-k", transport]
    return cmd


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
    return {
        "ok": Path(cfg.kd_exe).exists(),
        "kd_exe": cfg.kd_exe,
        "symbol_path": cfg.symbol_path,
        "state_file": cfg.state_file,
        "active_sessions": list(_sessions.keys()),
    }


@mcp.tool()
def start_kd(port: int | None = None, key: str | None = None, target: str | None = None) -> dict[str, Any]:
    """Start kd.exe for KDNET using explicit port/key values."""
    cfg = _load_config().kd
    port = port or cfg.default_port
    key = key or cfg.default_key
    cmd = _build_cmd(port, key, target)
    sess = _start_process(cmd)
    output = _wait_collect(sess, cfg.startup_timeout_sec, prompt_hint=False)
    if cfg.auto_initial_break:
        try:
            break_in(sess.id)
        except Exception:
            pass
    return {"session_id": sess.id, "pid": sess.process.pid, "command_line": cmd, "initial_output": output}


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
    res = start_kd(port=int(state["port"]), key=str(state["key"]), target=None)
    res["state_file"] = str(path)
    res["state"] = {k: v for k, v in state.items() if k != "key"}
    return res


@mcp.tool()
def kd_command(session_id: str, command: str, timeout_sec: int | None = None) -> dict[str, Any]:
    """Send a command to a running kd.exe session and collect output."""
    _check_command_allowed(command)
    sess = _session(session_id)
    if sess.process.poll() is not None:
        raise RuntimeError(f"KD process exited with code {sess.process.returncode}")
    assert sess.process.stdin is not None
    sess.process.stdin.write(command.rstrip("\n") + "\n")
    sess.process.stdin.flush()
    output = _wait_collect(sess, timeout_sec or _load_config().kd.command_timeout_sec)
    return {"ok": True, "session_id": session_id, "command": command, "output": output, "process_alive": sess.process.poll() is None}


@mcp.tool()
def break_in(session_id: str) -> dict[str, Any]:
    """Send a break signal to kd.exe. On Windows this uses CTRL_BREAK_EVENT when possible."""
    sess = _session(session_id)
    if sess.process.poll() is not None:
        raise RuntimeError("KD process is not running")
    if os.name == "nt":
        sess.process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    else:
        sess.process.send_signal(signal.SIGINT)
    output = _wait_collect(sess, 10, prompt_hint=False)
    return {"ok": True, "session_id": session_id, "output": output}


@mcp.tool()
def continue_go(session_id: str) -> dict[str, Any]:
    """Send the KD 'g' command to continue target execution."""
    return kd_command(session_id, "g", timeout_sec=5)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

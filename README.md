# kd-mcp

`kd-mcp` is an MCP server for controlling `kd.exe` on a Windows host.
It is designed to pair with a separate [`winrm-mcp`](../winrm-mcp) server for Windows guest VM setup over WinRM.

## Features

- Start `kd.exe` for KDNET kernel debugging.
- Start `kd.exe` from the shared state file written by `winrm-mcp`.
- Send KD commands and collect output.
- Break into the target.
- Continue target execution with `g`.
- Read buffered debugger output.
- Stop debugger sessions.
- List active sessions.
- Optional KD command allowlist/denylist policy.

## How it works with winrm-mcp

`winrm-mcp` configures the guest and writes a state file on the host:

```json
{
  "schema": "winrm-kd-session-v1",
  "guest_host": "192.168.122.50",
  "host_ip": "192.168.122.1",
  "port": 50000,
  "key": "1.2.3.4"
}
```

`kd-mcp` reads that same file with `start_from_state` and starts:

```text
kd.exe -k net:port=<PORT>,key=<KEY>
```

The two MCP servers do not need direct communication. The MCP client calls them in sequence.

## Requirements

Host:

- Windows 10/11 or Windows Server.
- Python 3.10+.
- Windows Kits Debuggers installed.
- Valid path to `kd.exe`, usually:

```text
C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kd.exe
```

Guest:

- KDNET already configured, usually via `winrm-mcp configure_kdnet`.
- Guest rebooted after debug boot settings were changed.

## Install

```powershell
git clone https://github.com/<you>/kd-mcp.git
cd kd-mcp
.\scripts\install.ps1
```

Edit `config.yaml` after installation.

## MCP client configuration

Example:

```json
{
  "mcpServers": {
    "kd": {
      "command": "C:\\tools\\kd-mcp\\.venv\\Scripts\\kd-mcp.exe",
      "env": {
        "KD_MCP_CONFIG": "C:\\tools\\kd-mcp\\config.yaml"
      }
    }
  }
}
```

## Tools

### `health_check()`

Checks config loading and verifies the `kd.exe` path exists.

### `start_kd(port, key, target)`

Starts `kd.exe` using explicit KDNET values.

### `start_from_state(state_file)`

Starts `kd.exe` using the shared state file written by `winrm-mcp`.

### `kd_command(session_id, command, timeout_sec)`

Sends a KD command and returns output.

Useful examples:

```text
lm
k
r
!process 0 0
!thread
!analyze -v
.reload /f
.symfix
.sympath
```

### `break_in(session_id)`

Sends a break signal to `kd.exe`.

### `continue_go(session_id)`

Sends `g` to continue target execution.

### `read_output(session_id, tail_chars)`

Returns buffered debugger output.

### `stop_kd(session_id, terminate_target)`

Stops the debugger process. By default, it does not intentionally terminate the debug target.

### `list_sessions()`

Lists active debugger sessions.

## End-to-end workflow with winrm-mcp

1. In `winrm-mcp/config.yaml` and `kd-mcp/config.yaml`, set the same state file:

```yaml
state_file: "C:\\mcp-state\\kd-session.json"
```

2. Use `winrm-mcp`:

```text
configure_kdnet
reboot
```

3. Use `kd-mcp`:

```text
start_from_state
kd_command: lm
kd_command: k
kd_command: !analyze -v
```

## Security notes

KD is powerful. A debugger command can inspect or alter kernel state.
Use this only on systems you own or are authorized to debug.
For shared environments, configure `allowed_command_prefixes` and keep `.shell` denied unless you explicitly need it.

## Troubleshooting

### `kd.exe not found`

Install Windows SDK Debugging Tools or update `kd.kd_exe` in `config.yaml`.

### KD waits forever

Confirm the guest rebooted after KDNET settings were applied. Also verify the host IP, firewall, port, and key.

### Symbols are missing

Check `symbol_path` and ensure the host can access the Microsoft symbol server or your local symbol cache.

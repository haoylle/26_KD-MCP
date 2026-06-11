# KD-MCP

`kd-mcp`는 Windows 커널 디버깅을 위해 `kd.exe` 또는 WinDbg를 제어하는 MCP(Model Context Protocol) 서버입니다.

이 프로젝트는 Windows 호스트에서 실행되며, KDNET을 통해 Windows 게스트 VM의 커널 디버깅 세션을 자동으로 연결하고 제어할 수 있습니다.

## Features

- KDNET 자동 연결
- kd.exe 제어
- WinDbg 제어
- 상태 파일 기반 자동 연결
- 디버거 명령 실행
- 디버거 출력 수집
- KD break 상태에서 WinRM 재시도를 돕는 `resume_for_winrm`
- hidden `kd.exe -server` 기반 remoting mode
- disposable `kd.exe -remote` command client
- visible remote client 열기 및 best-effort Ctrl+Break
- hidden server log tail 및 local kd process 조회
- 현재 연결 상태 조회
- 세션 종료
- WinRM-MCP 연동

## Architecture

KD-MCP는 항상 호스트 Windows에서 실행됩니다.

게스트 Windows에서는 KDNET 커널 디버깅만 활성화되어 있으면 됩니다.

```text
Host Windows
  ├─ Codex / MCP Client
  ├─ kd-mcp
  ├─ kd.exe
  └─ WinDbg

Guest Windows VM
  ├─ Windows Kernel
  └─ KDNET Target
```

## Integration With WinRM-MCP

KD-MCP는 WinRM-MCP와 함께 사용하는 것을 권장합니다.

WinRM-MCP는 게스트 VM 설정과 KDNET 활성화를 담당하고, KD-MCP는 실제 커널 디버거 연결과 제어를 담당합니다.

일반적인 작업 순서는 다음과 같습니다.

```text
1. WinRM-MCP 실행
2. configure_kdnet()
3. 게스트 재부팅
4. KD-MCP 실행
5. direct mode면 start_from_state()
6. remoting mode면 start_kd_server_from_state()
7. KDNET 연결
8. 커널 디버깅 시작
```

KD가 break 또는 breakpoint 상태에 들어가면 게스트 커널, 스케줄러, 네트워크 스택이 멈출 수 있습니다. 이 상태에서는 WinRM도 응답하지 않는 것이 정상입니다.

WinRM 작업이 timeout되거나 연결 실패가 발생하고 KD가 break 상태라면, 먼저 `resume_for_winrm` 또는 `continue_go`를 호출하여 `g` 명령을 실행하고 기본 5초 동안 기다린 뒤 WinRM-MCP의 `wait_for_winrm`으로 재연결을 확인하는 것이 안전합니다.

```text
1. kd-mcp: resume_for_winrm(session_id)
2. 약 5초 대기
3. winrm-mcp: wait_for_winrm
4. 이후 WinRM 작업 재시도
```

## State File

WinRM-MCP의 configure_kdnet()는 상태 파일을 생성합니다.

상태 파일에는 다음 정보가 포함됩니다.

- Guest Host
- Host IP
- KDNET Port
- KDNET Key
- 생성 시각

KD-MCP는 이 정보를 사용하여 자동으로 디버거를 연결합니다. 현재 구현은 `guest_host`를 `kd.exe`의 target 값에도 반영합니다.

직접 `kd.exe`를 붙이는 direct-session mode와 hidden `kd.exe -server` remoting mode를 둘 다 제공합니다. 같은 KDNET target에 두 mode를 동시에 owner로 붙이면 충돌할 수 있으므로 한 시점에는 하나만 사용해야 합니다.

## Requirements

호스트에는 다음이 필요합니다.

- Windows 10/11
- Windows Server
- Python 3.10 이상
- kd.exe 또는 WinDbg
- Debugging Tools for Windows

게스트에는 다음이 필요합니다.

- Windows 10/11
- Windows Server
- KDNET 지원 커널
- 네트워크 연결 가능 환경

## Installation

```powershell
git clone https://github.com/haoylle/26_KD-MCP.git
cd 26_KD-MCP
.\scripts\install.ps1
```

설치 후 상태 파일 경로와 디버거 실행 경로를 설정합니다.

## Configuration

KD-MCP는 상태 파일을 읽어 KDNET 연결을 구성합니다.

일반적으로 다음 정보를 사용합니다.

```json
{
  "schema": "winrm-kd-session-v1",
  "guest_host": "192.168.122.50",
  "host_ip": "192.168.122.1",
  "port": 50000,
  "key": "1.2.3.4"
}
```

WinRM-MCP와 KD-MCP는 동일한 포트, 키, 상태 파일을 사용해야 합니다.

`continue_wait_sec`는 `continue_go` 또는 `resume_for_winrm`이 `g` 명령을 보낸 뒤 기다리는 기본 시간입니다.

```yaml
kd:
  continue_wait_sec: 5.0
  server_port: 50055
  remote_server: "127.0.0.1"
  server_state_file: "C:\\mcp-state\\kd-server.json"
  workdir: "C:\\mcp-state\\kd-work"
```

## MCP Client Configuration

이 MCP 서버는 stdio 기반 로컬 MCP 서버로 실행됩니다.

아래 예시는 레포지토리를 `C:\tools\26_KD-MCP`에 설치했다고 가정합니다. 실제 경로에 맞게 수정해야 합니다.

### Claude Code

Claude Code에서 프로젝트 단위로 등록하려면 프로젝트 루트에서 다음 명령을 실행합니다.

```powershell
claude mcp add kd `
  --env KD_MCP_CONFIG="C:\tools\26_KD-MCP\config.yaml" `
  -- "C:\tools\26_KD-MCP\.venv\Scripts\kd-mcp.exe"
```

사용자 전체 설정으로 등록하고 싶다면 Claude Code의 MCP scope 옵션을 사용하여 user scope로 추가합니다.

```powershell
claude mcp add kd `
  --scope user `
  --env KD_MCP_CONFIG="C:\tools\26_KD-MCP\config.yaml" `
  -- "C:\tools\26_KD-MCP\.venv\Scripts\kd-mcp.exe"
```

수동으로 `.mcp.json`을 사용하는 경우에는 다음처럼 작성할 수 있습니다.

```json
{
  "mcpServers": {
    "kd": {
      "command": "C:\\tools\\26_KD-MCP\\.venv\\Scripts\\kd-mcp.exe",
      "env": {
        "KD_MCP_CONFIG": "C:\\tools\\26_KD-MCP\\config.yaml"
      }
    }
  }
}
```

등록 후 Claude Code를 다시 시작하거나 MCP 서버 목록을 갱신한 뒤 `kd.health_check`를 호출해 설정을 확인합니다.

### Codex CLI

Codex CLI에서는 사용자 설정 파일에 MCP 서버를 추가합니다.

Windows 기준 설정 파일 위치 예시는 다음과 같습니다.

```text
%USERPROFILE%\.codex\config.toml
```

다음 항목을 추가합니다.

```toml
[mcp_servers.kd]
command = "C:\\tools\\26_KD-MCP\\.venv\\Scripts\\kd-mcp.exe"
env = { KD_MCP_CONFIG = "C:\\tools\\26_KD-MCP\\config.yaml" }
```

Codex CLI를 다시 시작한 뒤 MCP tool 목록에서 `kd` 서버가 보이는지 확인합니다.

설정 확인은 다음 tool을 먼저 호출하는 방식으로 진행합니다.

```text
kd.health_check
```

## Tools

### start_from_state

상태 파일을 읽어 KDNET 연결을 시작합니다.

대부분의 사용자는 이 기능을 통해 자동 연결을 수행하면 됩니다. 상태 파일에 `guest_host`가 있으면 `kd.exe` 연결 인자에 같이 반영합니다.

### start_kd

포트, 키, 타겟 값을 직접 지정하여 KDNET 연결을 수행합니다.

### start_kd_server / start_kd_server_from_state

hidden `kd.exe -server` owner를 띄운 뒤 `kd.exe -remote` 클라이언트로 제어하는 mode입니다.

이 mode는 사람이 visible remote client를 열어도 hidden owner가 계속 유지되는 점이 장점입니다.

### kd_command

kd.exe 또는 WinDbg에 디버거 명령을 전달합니다.

현재 구현은 `kd>` 프롬프트를 줄바꿈 없이 출력하는 경우도 처리하도록 문자 단위로 출력을 수집합니다.

예시는 다음과 같습니다.

```text
!process 0 0
lm
!thread
k
```

### break_in

실행 중인 KD 세션에 break 신호를 보냅니다.

break 상태에서는 게스트 커널과 네트워크 스택이 멈출 수 있으므로 WinRM이 timeout되는 것이 정상일 수 있습니다.

도구 결과에는 `prompt_seen`이 포함되며, 실제로 `kd>` 프롬프트를 감지했는지 확인할 수 있습니다.

### continue_go

KD 세션에 `g` 명령을 보내 게스트 실행을 재개합니다.

`wait_sec` 값을 생략하면 설정 파일의 `kd.continue_wait_sec` 값만큼 기다린 뒤 반환합니다.

### resume_for_winrm

KD break 또는 bp 상태에서 WinRM을 다시 사용해야 할 때 호출하는 helper입니다.

내부적으로 `g` 명령을 실행하고 기본 5초 동안 기다린 뒤, WinRM-MCP의 `wait_for_winrm` 호출을 권장하는 결과를 반환합니다.

### read_output

현재 KD 세션의 출력 버퍼를 반환합니다.

### kd_server_command / kd_server_script

hidden remoting server에 disposable remote client를 붙여 명령이나 스크립트를 실행합니다.

`g` 같은 long-running command는 다음 break 전까지 반환되지 않을 수 있으므로 timeout에 걸릴 수 있습니다.

### kd_server_status / read_kd_server_log / list_kd_processes

hidden remoting server의 상태, 로그, 로컬 `kd.exe`/`windbg.exe` 프로세스를 조회합니다.

### open_remote_client / break_remote_client

visible `kd.exe -remote` 창을 열거나, 그 창을 통해 best-effort Ctrl+Break를 보냅니다.

이 방식은 hidden server가 session owner를 유지한 채로 사람이 수동으로 들여다볼 수 있게 해 줍니다.

### stop_kd

현재 디버깅 세션을 종료합니다.

### list_sessions

현재 활성 KD 세션 목록을 반환합니다.

## Common Commands

커널 디버깅 시 자주 사용하는 명령은 다음과 같습니다.

```text
!process 0 0
!thread
!handle
!pool
!pte
lm
k
r
```

필요에 따라 WinDbg 확장 명령도 사용할 수 있습니다.

## Troubleshooting

### Cannot connect to KDNET target

다음을 확인합니다.

- KDNET 설정 적용 여부
- 게스트 재부팅 여부
- 포트 번호
- KDNET Key
- 방화벽 설정

### WinRM does not respond while KD is attached

KD가 단순히 연결되어 있는 상태라면 WinRM은 동작할 수 있습니다.

하지만 KD가 break 상태이거나 breakpoint에 걸려 `kd>` 프롬프트에서 멈춰 있으면 게스트 커널과 네트워크 스택이 멈추므로 WinRM이 응답하지 않을 수 있습니다.

이 경우 다음 순서로 처리합니다.

```text
1. resume_for_winrm(session_id)
2. 5초 정도 대기
3. winrm-mcp: wait_for_winrm
4. WinRM 작업 재시도
```

remoting mode의 `kd_server_command("g")`는 다음 break까지 remote client가 반환되지 않을 수 있으므로, WinRM 복구 자동화가 중요하면 direct-session mode의 `resume_for_winrm`이 더 적합합니다.

### Waiting to reconnect

게스트가 아직 재부팅 중이거나 KDNET 설정이 적용되지 않았을 수 있습니다.

게스트에서 다음 명령을 확인합니다.

```powershell
bcdedit /dbgsettings
bcdedit /enum {current}
```

### State file not found

WinRM-MCP의 configure_kdnet()가 상태 파일을 생성했는지 확인합니다.

두 MCP 서버가 동일한 상태 파일 경로를 사용해야 합니다.

## Security Notes

KDNET 연결 정보는 신뢰할 수 있는 환경에서만 사용해야 합니다.

상태 파일에는 디버깅 연결 정보가 포함되어 있으므로 접근 권한을 적절히 제한하는 것이 좋습니다.

## License

MIT License

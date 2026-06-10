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
5. start_from_state()
6. KDNET 연결
7. 커널 디버깅 시작
```

## State File

WinRM-MCP의 configure_kdnet()는 상태 파일을 생성합니다.

상태 파일에는 다음 정보가 포함됩니다.

- Guest Host
- Host IP
- KDNET Port
- KDNET Key
- 생성 시각

KD-MCP는 이 정보를 사용하여 자동으로 디버거를 연결합니다.

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

## MCP Client Configuration

```json
{
  "mcpServers": {
    "kd": {
      "command": "C:\\tools\\26_KD-MCP\\.venv\\Scripts\\kd-mcp.exe"
    }
  }
}
```

## Tools

### start_from_state

상태 파일을 읽어 KDNET 연결을 시작합니다.

대부분의 사용자는 이 기능을 통해 자동 연결을 수행하면 됩니다.

### connect

IP, 포트, 키를 직접 지정하여 KDNET 연결을 수행합니다.

### kd_command

kd.exe 또는 WinDbg에 디버거 명령을 전달합니다.

예시는 다음과 같습니다.

```text
!process 0 0
lm
!thread
k
```

### get_status

현재 디버거 연결 상태를 반환합니다.

### disconnect

현재 디버깅 세션을 종료합니다.

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

$ErrorActionPreference = "Stop"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
  throw "Python launcher 'py' was not found. Install Python 3.10+ first."
}

py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -e .

if (-not (Test-Path .\config.yaml)) {
  Copy-Item .\examples\config.yaml .\config.yaml
  Write-Host "Created config.yaml from examples/config.yaml. Edit it before running."
}

Write-Host "Installed kd-mcp. Binary: $PWD\.venv\Scripts\kd-mcp.exe"

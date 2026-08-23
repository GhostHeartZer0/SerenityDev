$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

if (Test-Path "$ScriptDir\.venv\Scripts\Activate.ps1") {
    & "$ScriptDir\.venv\Scripts\Activate.ps1"
} elseif (Test-Path "$ScriptDir\venv\Scripts\Activate.ps1") {
    & "$ScriptDir\venv\Scripts\Activate.ps1"
}

python start_native_mcp.py @args

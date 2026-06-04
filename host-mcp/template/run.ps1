#requires -Version 5
# Launch the host build MCP on Windows.
#
# Run this from a "Developer PowerShell for VS 2022" so cmake/msbuild/ctest are
# on PATH. It creates a local venv, installs requirements, and starts the
# server. Keep this window open while the agent works.
#
#   ./run.ps1            # uses the port in project.json
#   $env:HOST_MCP_PORT=9123; ./run.ps1   # override the port

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python = "python"
if (-not (Get-Command $python -ErrorAction SilentlyContinue)) { $python = "py" }

if (-not (Test-Path ".venv")) {
    Write-Host "[host-mcp] creating venv..."
    & $python -m venv .venv
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt

Write-Host "[host-mcp] starting server (Ctrl+C to stop)..."
& $venvPython server.py

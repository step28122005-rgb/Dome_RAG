param([int]$Port = 8780, [string]$Url = '')
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Install first: python -m venv .venv, then .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
$clientArgs = @((Join-Path $PSScriptRoot 'dome_web.py'), '--port', $Port)
if ($Url) { $clientArgs += @('--url', $Url) }
& $pythonPath @clientArgs
exit $LASTEXITCODE

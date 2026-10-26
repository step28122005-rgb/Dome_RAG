param(
    [string]$Url = '',
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Install first: python -m venv .venv, then .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
$clientArgs = @((Join-Path $PSScriptRoot 'dome_client.py'))
if ($Url) { $clientArgs += @('--url', $Url) }
if ($Once) { $clientArgs += '--once' }
& $pythonPath @clientArgs
exit $LASTEXITCODE

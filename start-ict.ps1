param(
    [ValidateRange(1,65535)][Nullable[int]]$Port = $null,
    [string]$HostAddress = $null,
    [switch]$WebsiteOnly,
    [switch]$EnableWechat
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if ($WebsiteOnly -and $EnableWechat) {
    throw 'Use either -WebsiteOnly or -EnableWechat.'
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create .venv with Python 3.12 and install requirements.txt first.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
# The ordinary launch starts only this website. Enabling WeChat is explicit and
# never installs a gateway, stops a listener, or switches an existing project.
$env:ENABLE_WECHAT = if ($EnableWechat) { 'true' } else { 'false' }
if ($null -ne $Port) { $env:VERA_PORT = [string]$Port }
if ($HostAddress) { $env:VERA_HOST = $HostAddress }

& $python -B (Join-Path $PSScriptRoot 'main.py')
if ($LASTEXITCODE -ne 0) {
    throw 'Vera exited with an error. Check the startup message and configuration.'
}

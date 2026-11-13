param([ValidateSet('gui','download','install')][string]$Action='gui')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
$env:UV_CACHE_DIR=Join-Path $root '.uv-cache'
$env:UV_DEFAULT_INDEX='https://pypi.org/simple'
$uv=Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { $uvExe=$uv.Source } else {
    $uvExe=Join-Path $root '.tools\uv\uv.exe'
    if (-not (Test-Path -LiteralPath $uvExe)) {
        $env:UV_INSTALL_DIR=Join-Path $root '.tools\uv'
        $env:UV_NO_MODIFY_PATH='1'
        [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
        $installer=Join-Path $env:TEMP ('cfimage-uv-'+[Guid]::NewGuid().ToString('N')+'.ps1')
        Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1' -OutFile $installer
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw 'uv installation failed' }
    }
}
& $uvExe sync --locked --python 3.12
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed; check internet access' }
if ($Action -eq 'install') { exit 0 }
if ($Action -eq 'download') { & $uvExe run --no-sync python -X utf8 -m src.nas_transfer download }
else { & $uvExe run --no-sync python -X utf8 -m src.launcher }
exit $LASTEXITCODE

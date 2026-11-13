param([ValidateSet('gui','download','install','upload')][string]$Action='gui')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
$env:UV_CACHE_DIR=Join-Path $root '.uv-cache'
$env:UV_PYTHON_DOWNLOADS='never'
$env:PYTHONUTF8='1'
$tools=Join-Path $root '.tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$guard=$null
try {
    try { $guard=[IO.File]::Open((Join-Path $tools 'bootstrap.lock'),'OpenOrCreate','ReadWrite','None') }
    catch { throw 'Another environment setup is running. Close it before retrying.' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    $cfg=Get-Content -Raw -LiteralPath (Join-Path $root 'config/runtime-bootstrap.json') | ConvertFrom-Json
    if ($cfg.schema_version -ne 1 -or $cfg.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Invalid runtime manifest.' }
    $requirements=Join-Path $root 'config/requirements-runtime.txt'
    if ((Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash -ne $cfg.requirements_sha256 -or
        (Get-FileHash -LiteralPath (Join-Path $root 'uv.lock') -Algorithm SHA256).Hash -ne $cfg.uv_lock_sha256) {
        throw 'Code and runtime pins do not match. Update the complete code package.'
    }
    $runtime=Join-Path $tools ('runtime-'+$cfg.sha256.Substring(0,16))
    if (-not (Test-Path -LiteralPath (Join-Path $runtime 'ready'))) {
        $archive=Join-Path $tools ($cfg.sha256+'.zip')
        $valid=(Test-Path -LiteralPath $archive) -and ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -eq $cfg.sha256)
        if (-not $valid) {
            foreach ($url in $cfg.urls) {
                $client=New-Object Net.WebClient
                $client.Proxy=$null
                try {
                    Write-Host ('[1/3] Downloading runtime from LAN NAS ({0:N0} MiB)...' -f ($cfg.size/1MB))
                    $task=$client.DownloadFileTaskAsync([Uri]$url, ($archive+'.part'))
                    $timer=[Diagnostics.Stopwatch]::StartNew()
                    while (-not $task.IsCompleted) {
                        Start-Sleep -Seconds 1
                        if ($timer.Elapsed.TotalSeconds -gt 300) { $client.CancelAsync(); throw 'NAS download timed out.' }
                        $part=Get-Item -LiteralPath ($archive+'.part') -ErrorAction SilentlyContinue
                        if ($part) { Write-Progress -Activity 'Downloading runtime from NAS' -Status ('{0:N1} / {1:N1} MiB' -f ($part.Length/1MB),($cfg.size/1MB)) -PercentComplete ([Math]::Min(100,100*$part.Length/$cfg.size)) }
                    }
                    $task.GetAwaiter().GetResult()
                    Write-Progress -Activity 'Downloading runtime from NAS' -Completed
                    if ((Get-FileHash -LiteralPath ($archive+'.part') -Algorithm SHA256).Hash -ne $cfg.sha256) { throw 'Runtime SHA-256 mismatch.' }
                    Move-Item -LiteralPath ($archive+'.part') -Destination $archive -Force
                    $valid=$true
                    break
                } catch { Write-Warning $_.Exception.Message }
                finally { $client.Dispose() }
            }
        }
        if (-not $valid) { throw 'Cannot download verified runtime. Connect to the lab LAN/VPN and retry. No internet package installation was attempted.' }
        Write-Host '[2/3] Extracting verified Python, uv and dependency wheels...'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $staging=Join-Path $tools ('extract-'+[Guid]::NewGuid().ToString('N'))
        [IO.Compression.ZipFile]::ExtractToDirectory($archive,$staging)
        if (-not (Test-Path -LiteralPath (Join-Path $staging 'python/python.exe'))) { throw 'Runtime archive is incomplete.' }
        Set-Content -LiteralPath (Join-Path $staging 'ready') -Value $cfg.sha256 -Encoding ASCII
        Move-Item -LiteralPath $staging -Destination $runtime
    }
    $uvExe=Join-Path $runtime 'uv.exe'
    $python=Join-Path $root '.venv/Scripts/python.exe'
    $bundledPython=Join-Path $runtime 'python/python.exe'
    if (Test-Path -LiteralPath $python) {
        $base=& $python -c 'import sys; print(sys.base_prefix)'
        if ($LASTEXITCODE -ne 0 -or $base -ne (Split-Path $bundledPython -Parent)) {
            # Keep a recoverable backup, and avoid early Python 3.12/Qt import bugs.
            $old=[IO.Path]::GetFullPath((Join-Path $root '.venv'))
            $backup=[IO.Path]::GetFullPath((Join-Path $tools ('previous-venv-'+[Guid]::NewGuid().ToString('N'))))
            $boundary=[IO.Path]::GetFullPath($root).TrimEnd('\')+'\'
            if (-not $old.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase) -or
                -not $backup.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe environment backup path.' }
            Write-Host 'Preserving previous environment; switching to the bundled Python version...'
            Move-Item -LiteralPath $old -Destination $backup
        }
    }
    if (-not (Test-Path -LiteralPath $python)) {
        & $uvExe venv --python $bundledPython (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'uv could not create the project environment.' }
    }
    & $python -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) and sys.maxsize>2**32 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv is not a working Python 3.12 x64 environment. Preserve it under another name, then retry.' }
    Write-Host '[3/3] Installing pinned dependencies with uv (offline, no PyPI downloads)...'
    $extra=@()
    if (Get-ChildItem -Path (Join-Path $root '.venv/Lib/site-packages/pyside6-*.dist-info') -ErrorAction SilentlyContinue) { $extra=@('--reinstall-package','pyside6-essentials') }
    & $uvExe pip sync --python $python --offline --no-index --find-links (Join-Path $runtime 'wheels') --require-hashes $requirements @extra
    if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed. Close running experiment windows and retry.' }
    & $python -c 'from PySide6 import QtCore,QtGui,QtWidgets,QtOpenGLWidgets; import numpy, PIL, boto3, bc_ecap_sdk'
    if ($LASTEXITCODE -ne 0) { throw 'Runtime import check failed.' }
    Write-Host 'Environment ready.'
} finally { if ($guard) { $guard.Dispose() } }
$authorizationPackage=Join-Path $root '.dispatch/upload-authorization.json'
if (Test-Path -LiteralPath $authorizationPackage) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts/install_upload_authorization.ps1') -Package $authorizationPackage
    if ($LASTEXITCODE -ne 0) { Write-Host 'Acquisition can continue; upload authorization still needs repair.' }
}
if ($Action -eq 'install') { exit 0 }
if ($Action -eq 'download') { & $python -X utf8 -m src.nas_transfer download }
elseif ($Action -eq 'upload') { & $python -X utf8 -m src.upload_gui }
else { & $python -X utf8 -m src.launcher }
exit $LASTEXITCODE

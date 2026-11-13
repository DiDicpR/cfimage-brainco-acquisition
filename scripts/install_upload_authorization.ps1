param([Parameter(Mandatory=$true)][string]$Package, [switch]$KeepPackage)
$ErrorActionPreference='Stop'
try {
    Add-Type -AssemblyName System.Security
    $envelope=Get-Content -Raw -LiteralPath $Package -Encoding UTF8 | ConvertFrom-Json
    if ($envelope.schema_version -ne 1) { throw 'Invalid authorization package.' }
    # Portable installation material is recoverable by anyone holding this package.
    # Only the installed DPAPI file is bound to this Windows user and machine.
    $keys=[Convert]::FromBase64String($envelope.keys)
    $iv=[Convert]::FromBase64String($envelope.iv)
    $cipher=[Convert]::FromBase64String($envelope.ciphertext)
    if ($keys.Length -ne 64 -or $iv.Length -ne 16) { throw 'Invalid authorization package.' }
    $mac=New-Object Security.Cryptography.HMACSHA256
    $mac.Key=[byte[]]$keys[32..63]
    $actual=[Convert]::ToBase64String($mac.ComputeHash([byte[]]($iv+$cipher)))
    $mac.Dispose()
    if ($actual -cne $envelope.mac) { throw 'Authorization package verification failed.' }
    $aes=[Security.Cryptography.Aes]::Create()
    $aes.Key=[byte[]]$keys[0..31];$aes.IV=$iv
    $decrypt=$aes.CreateDecryptor()
    $plain=$decrypt.TransformFinalBlock($cipher,0,$cipher.Length)
    $decrypt.Dispose();$aes.Dispose()
    $value=[Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
    if ($value.kind -eq 'smb') {
        if ($value.schema_version -ne 1 -or $value.share -ne '\\172.16.4.2\aim' -or -not $value.username -or -not $value.password) { throw 'Unexpected SMB authorization format.' }
        $credentialName='smb-upload.dpapi'
    } else {
    if ($value.schema_version -ne 1 -or $value.endpoint -ne 'http://172.16.4.2:9000' -or
        $value.data_bucket -ne 'oi-data' -or $value.prefix -ne 'CFIMAGE-20261113' -or
        -not $value.access_key -or -not $value.secret_key) { throw 'Unexpected upload destination or authorization format.' }
        $credentialName='s3-upload.dpapi'
    }
    $protected=[Security.Cryptography.ProtectedData]::Protect($plain,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    $directory=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CFIMAGE'
    [void][IO.Directory]::CreateDirectory($directory)
    $destination=Join-Path $directory $credentialName
    $temporary=$destination+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllBytes($temporary,$protected)
    # Verify before replacing a previously working authorization.
    $roundtrip=[Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($temporary),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    if ([Convert]::ToBase64String($plain) -cne [Convert]::ToBase64String($roundtrip)) { throw 'DPAPI readback failed.' }
    Move-Item -LiteralPath $temporary -Destination $destination -Force
    if (-not $KeepPackage) { Remove-Item -LiteralPath $Package -Force }
    Write-Host 'Upload authorization installed for this Windows user. Automatic NAS login is ready.'
    exit 0
} catch {
    # Exception details from JSON/crypto must never echo credential values.
    Write-Host 'Upload authorization installation failed. Keep the package and contact the operator.'
    exit 1
} finally {
    if ($plain) { [Array]::Clear($plain,0,$plain.Length) }
    if ($roundtrip) { [Array]::Clear($roundtrip,0,$roundtrip.Length) }
    if ($keys) { [Array]::Clear($keys,0,$keys.Length) }
}

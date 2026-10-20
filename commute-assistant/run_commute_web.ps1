$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$secretPath = Join-Path $root ".local_secrets\amap.dpapi"
$pythonPath = Join-Path $root ".venv\Scripts\python.exe"
$scriptPath = Join-Path $root "commute_web.py"
$keyPtr = [IntPtr]::Zero

try {
    if (-not (Test-Path $secretPath)) {
        throw "AMap key file is missing. Run the commute schedule setup first."
    }
    if (-not (Test-Path $pythonPath)) {
        throw "Project Python environment was not found: $pythonPath"
    }
    $keySecure = (Get-Content -Raw $secretPath).Trim() | ConvertTo-SecureString
    $keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($keySecure)
    $env:AMAP_WEB_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr)
    & $pythonPath $scriptPath
    if ($LASTEXITCODE -ne 0) {
        throw "Commute route helper exited with code $LASTEXITCODE"
    }
}
finally {
    if ($keySecure) { $keySecure.Dispose() }
    if ($keyPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
    }
    Remove-Item Env:AMAP_WEB_KEY -ErrorAction SilentlyContinue
}

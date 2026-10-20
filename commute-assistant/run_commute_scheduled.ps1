param([switch]$Scheduled)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$secretDir = Join-Path $root ".local_secrets"
$logDir = Join-Path $root "commute_logs"
$logPath = Join-Path $logDir "scheduled_runs.log"
$openAiSecretPath = Join-Path $secretDir "openai.dpapi"
$amapSecretPath = Join-Path $secretDir "amap.dpapi"
$pythonPath = Join-Path $root ".venv\Scripts\python.exe"
$scriptPath = Join-Path $root "commute_assistant.py"

if ($Scheduled) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 开始 ===" | Out-File -FilePath $logPath -Append -Encoding utf8
}

$openAiPtr = [IntPtr]::Zero
$amapPtr = [IntPtr]::Zero
$exitCode = 1
$previousErrorActionPreference = $ErrorActionPreference
try {
    if (-not (Test-Path $openAiSecretPath) -or -not (Test-Path $amapSecretPath)) {
        throw "找不到本机加密保存的 API Key。请先运行 setup_commute_schedule.ps1 完成首次设置。"
    }
    $openAiSecure = (Get-Content -Raw $openAiSecretPath).Trim() | ConvertTo-SecureString
    $amapSecure = (Get-Content -Raw $amapSecretPath).Trim() | ConvertTo-SecureString
    $openAiPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($openAiSecure)
    $amapPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($amapSecure)
    $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($openAiPtr)
    $env:AMAP_WEB_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($amapPtr)
    # Native Python errors arrive on PowerShell's error stream. Keep them non-terminating
    # so the complete traceback is captured instead of only the first line.
    $ErrorActionPreference = "Continue"
    if ($Scheduled) {
        $pythonOutput = & $pythonPath $scriptPath 2>&1
        $exitCode = $LASTEXITCODE
        $pythonOutput | Out-File -FilePath $logPath -Append -Encoding utf8
    }
    else {
        & $pythonPath $scriptPath
        $exitCode = $LASTEXITCODE
    }
    $ErrorActionPreference = $previousErrorActionPreference
    if ($openAiSecure) { $openAiSecure.Dispose() }
    if ($amapSecure) { $amapSecure.Dispose() }
}
catch {
    if ($Scheduled) {
        "运行失败：$($_.Exception.Message)" | Out-File -FilePath $logPath -Append -Encoding utf8
    }
    else {
        Write-Error "运行失败：$($_.Exception.Message)"
    }
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    if ($openAiPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($openAiPtr) }
    if ($amapPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($amapPtr) }
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:AMAP_WEB_KEY -ErrorAction SilentlyContinue
    if ($Scheduled) {
        "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 结束（退出码：$exitCode） ===`r`n" | Out-File -FilePath $logPath -Append -Encoding utf8
    }
}
exit $exitCode

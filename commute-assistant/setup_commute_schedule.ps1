$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$secretDir = Join-Path $root ".local_secrets"
$runnerPath = Join-Path $root "run_commute_scheduled.ps1"
$pythonPath = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonPath)) {
    throw "没有找到项目虚拟环境：$pythonPath。请先确认通勤助手可以手动运行。"
}

Write-Host "请输入 OpenAI API Key。输入内容不会显示，并将由当前 Windows 账户加密保存。"
$openAiSecure = Read-Host "OpenAI API Key" -AsSecureString
Write-Host "请输入高德 Web 服务 Key。输入内容不会显示，并将由当前 Windows 账户加密保存。"
$amapSecure = Read-Host "高德 Web 服务 Key" -AsSecureString

New-Item -ItemType Directory -Path $secretDir -Force | Out-Null
$openAiSecure | ConvertFrom-SecureString | Set-Content -Path (Join-Path $secretDir "openai.dpapi") -Encoding ASCII
$amapSecure | ConvertFrom-SecureString | Set-Content -Path (Join-Path $secretDir "amap.dpapi") -Encoding ASCII
$openAiSecure.Dispose()
$amapSecure.Dispose()

$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -Scheduled"
$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $arguments -WorkingDirectory $root
$morningTrigger = New-ScheduledTaskTrigger -Daily -At "8:00AM"
$eveningTrigger = New-ScheduledTaskTrigger -Daily -At "6:00PM"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "CommuteAssistantDaily" -Description "每天早上8点和下午6点生成上海自驾通勤简报" -Action $action -Trigger @($morningTrigger, $eveningTrigger) -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "设置完成：计划任务已创建，每天 08:00 和 18:00 运行。"
Write-Host "报告保存在：$(Join-Path $root 'commute_reports')"
Write-Host "运行记录保存在：$(Join-Path $root 'commute_logs\scheduled_runs.log')"
Write-Host "如果电脑在计划时间关机，任务会在下次可运行时补跑；实际路况查询时间以报告为准。"

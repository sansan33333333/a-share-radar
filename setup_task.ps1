# 注册 Windows 计划任务：交易时段每 30 分钟自动分析
# 用法：右键“使用 PowerShell 运行”，或在 PowerShell 中执行
#   powershell -ExecutionPolicy Bypass -File .\setup_task.ps1

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 优先用项目虚拟环境，其次用 PATH 里的 pythonw
$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) { throw "未找到 pythonw.exe，请先安装 Python 或设置 PATH" }

$pipeline = Join-Path $dir "pipeline.py"
$server = Join-Path $dir "server.py"

# 1) 分析任务：每天 09:30 起，每 30 分钟，持续 6 小时（脚本会自动跳过午休/休市）
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$pipeline`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At "09:30"
$repetition = (New-ScheduledTaskTrigger -Once -At "09:30" `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 6)).Repetition
$trigger.Repetition = $repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "AStock_Radar_30min" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "A股短线雷达：交易时段每30分钟抓取全A数据、评分并调用模型研判" | Out-Null

Write-Host "[OK] 已注册任务 AStock_Radar_30min（交易日 09:30-15:00，每30分钟）" -ForegroundColor Green

# 2) 看板服务开机自启：写入当前用户 Run 键（无需管理员权限，已自动配置）
$startupCmd = '"' + $pythonw + '" "' + (Join-Path $dir "server.py") + '"'
New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "AStockRadar" -Value $startupCmd -PropertyType String -Force | Out-Null
Write-Host "[OK] 看板服务已加入开机自启（HKCU Run: AStockRadar）" -ForegroundColor Green

Write-Host ""
Write-Host "查看任务：Get-ScheduledTask -TaskName 'AStock_Radar_*'" -ForegroundColor Cyan
Write-Host "立即执行：Start-ScheduledTask -TaskName 'AStock_Radar_30min'" -ForegroundColor Cyan
Write-Host "删除任务：powershell -ExecutionPolicy Bypass -File .\remove_task.ps1" -ForegroundColor Cyan

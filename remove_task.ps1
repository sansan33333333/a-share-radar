# 删除本系统注册的计划任务
$ErrorActionPreference = "SilentlyContinue"
foreach ($name in @("AStock_Radar_30min", "AStock_Radar_Server")) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "[OK] 已删除 $name"
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "AStockRadar" -ErrorAction SilentlyContinue
Write-Host "[OK] 已移除开机自启 AStockRadar"

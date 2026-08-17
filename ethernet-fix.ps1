# Intel I226-V: Energy Efficient Ethernet ve cihaz güç tasarrufunu kapatır.
# Sağ tık → PowerShell ile çalıştır. Yönetici isteği çıkmazsa:
# Başlat → PowerShell → sağ tık → Yönetici olarak çalıştır → bu dosyayı sürükleyip Enter.

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

Write-Host "Ethernet enerji tasarrufu kapatiliyor..." -ForegroundColor Yellow

Set-NetAdapterAdvancedProperty -Name "Ethernet" -DisplayName "Energy Efficient Ethernet" -DisplayValue "Off"

$nic = Get-CimInstance MSPower_DeviceEnable -Namespace root\wmi |
    Where-Object { $_.InstanceName -match "VEN_8086.*DEV_125C" }
if ($nic) {
    $nic.Enable = $false
    Set-CimInstance -CimInstance $nic
}

$eee = Get-NetAdapterAdvancedProperty -Name "Ethernet" -DisplayName "Energy Efficient Ethernet"
Write-Host "Energy Efficient Ethernet:" $eee.DisplayValue -ForegroundColor Green
if ($nic) {
    $check = Get-CimInstance MSPower_DeviceEnable -Namespace root\wmi |
        Where-Object { $_.InstanceName -match "VEN_8086.*DEV_125C" }
    Write-Host "Cihaz guc tasarrufu (Enable):" $check.Enable -ForegroundColor Green
}

Write-Host "Tamam. PC'yi bir kez yeniden baslatin." -ForegroundColor Green
pause

# 查询摄像头的所有设备属性
Write-Host "=== 设备属性 ==="
$dev = Get-PnpDevice -FriendlyName "*Camera*" | Select-Object -First 1
Write-Host "设备: $($dev.FriendlyName)"
Write-Host "ID:   $($dev.InstanceId)"

Write-Host "`n=== 设备驱动信息 ==="
$driv = Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.DeviceID -eq $dev.DeviceID }
if ($driv) {
    Write-Host "驱动版本: $($driv.DriverVersion)"
    Write-Host "驱动日期: $($driv.DriverDate)"
    Write-Host "驱动名称: $($driv.InfName)"
    Write-Host "驱动供应商: $($driv.Manufacturer)"
}

Write-Host "`n=== 设备管理器属性 ==="
$pnpProps = Get-PnpDeviceProperty -InstanceId $dev.InstanceId -ErrorAction SilentlyContinue
foreach ($p in $pnpProps) {
    $key = $p.KeyName
    $val = $p.Data
    if ($val -and $key -match 'DEVPKEY_Device|DEVPKEY_Driver') {
        Write-Host "  $key = $val"
    }
}

Write-Host "`n=== 摄像头相关注册表 ==="
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{6bdd1fc6-810f-11d0-bec7-08002be2092f}"
Get-ChildItem $regPath -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
    if ($props.PSObject.Properties.Name -match '04CA|7086|Camera') {
        Write-Host "`n$($_.PSPath)"
        $props.PSObject.Properties | ForEach-Object {
            Write-Host "  $($_.Name) = $($_.Value)"
        }
    }
}

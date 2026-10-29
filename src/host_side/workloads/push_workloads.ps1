$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

adb root | Out-Null
adb wait-for-device
adb shell "rm -rf /data/local/tmp/workloads && mkdir -p /data/local/tmp/workloads /data/local/tmp/radar_registry /data/local/tmp/radar_truth"

Get-ChildItem -Path $here -Filter "*.sh" | ForEach-Object {
    $content = [System.IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n"
    $tempFile = Join-Path $env:TEMP $_.Name
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempFile, $content, $utf8NoBom)

    Write-Host "Pushing $($_.Name) -> /data/local/tmp/workloads/$($_.Name)"
    adb push $tempFile "/data/local/tmp/workloads/$($_.Name)" | Out-Null
    Remove-Item $tempFile -Force
}

adb shell "chmod 755 /data/local/tmp/workloads/*.sh"
Write-Host "`nAll workloads pushed and chmod 755 completed."
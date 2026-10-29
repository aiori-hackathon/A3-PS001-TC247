param(
    [string]$Root = "/data/media/0",
    [int]$WindowMs = 5000,
    [int]$MinFiles = 6,
    [double]$MinScore = 15.0,
    [switch]$StopOnly
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

adb root | Out-Null
adb wait-for-device

# Stop any existing MonitorKt process on the emulator
adb shell "pkill -f MonitorKt 2>/dev/null || true"
if ($StopOnly) {
    Write-Host "Stopped MonitorKt daemon on emulator."
    exit 0
}

$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } elseif ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } else { Join-Path $env:LOCALAPPDATA "Android\Sdk" }
$d8 = Get-ChildItem (Join-Path $sdk "build-tools") -Directory | Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName "d8.bat" } | Where-Object { Test-Path $_ } | Select-Object -First 1
$androidJar = Get-ChildItem (Join-Path $sdk "platforms") -Directory | Where-Object { $_.Name -like "android-*" } | Sort-Object { [int](($_.Name -replace "[^0-9]", "0")) } -Descending | ForEach-Object { Join-Path $_.FullName "android.jar" } | Where-Object { Test-Path $_ } | Select-Object -First 1

New-Item -ItemType Directory -Force build | Out-Null
Write-Host "[1/3] Compiling Monitor.kt..."
kotlinc Monitor.kt -cp $androidJar -include-runtime -d build\monitor_raw.jar
& $d8 --release --min-api 33 --lib $androidJar --output build\radar.jar build\monitor_raw.jar

Write-Host "[2/3] Pushing /data/local/tmp/radar.jar and preparing log files..."
adb push build\radar.jar /data/local/tmp/radar.jar | Out-Null
adb shell "mkdir -p /data/local/tmp/radar_logs /data/local/tmp/radar_registry /data/local/tmp/radar_truth && touch /data/local/tmp/radar_logs/events.jsonl /data/local/tmp/radar_logs/alerts.jsonl /data/local/tmp/radar_logs/kill.jsonl /data/local/tmp/radar_logs/health.log"

Write-Host "[3/3] Starting MonitorKt as an independent background daemon on Android..."
adb shell "nohup sh -c 'CLASSPATH=/data/local/tmp/radar.jar app_process /system/bin MonitorKt --root $Root --window-ms $WindowMs --min-files $MinFiles --min-score $MinScore --kill true >/dev/null 2>&1' >/dev/null 2>&1 &"
Start-Sleep -Seconds 1

$runningPid = (adb shell "pidof app_process" 2>$null).Trim()
Write-Host "MonitorKt daemon is RUNNING on emulator (PID: $runningPid)"
Write-Host "Logs are being written locally on device to /data/local/tmp/radar_logs/"
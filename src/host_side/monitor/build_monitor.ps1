param(
    [switch]$SelfTest,
    [switch]$CheckEnv,
    [switch]$CheckDirs,
    [string]$Root = "/data/media/0",
    [int]$WindowMs = 5000,
    [int]$MinFiles = 6,
    [double]$MinScore = 15.0
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

foreach ($tool in "kotlinc", "adb") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool not found on PATH" }
}

$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) { $sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $sdk)) { throw "Android SDK not found. Set ANDROID_HOME." }

$d8 = Get-ChildItem (Join-Path $sdk "build-tools") -Directory |
      Sort-Object Name -Descending |
      ForEach-Object { Join-Path $_.FullName "d8.bat" } |
      Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $d8) { throw "d8.bat not found under $sdk\build-tools" }

$androidJar = Get-ChildItem (Join-Path $sdk "platforms") -Directory |
      Where-Object { $_.Name -like "android-*" } |
      Sort-Object { [int](($_.Name -replace "[^0-9]", "0")) } -Descending |
      ForEach-Object { Join-Path $_.FullName "android.jar" } |
      Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $androidJar) { throw "android.jar not found under $sdk\platforms" }

New-Item -ItemType Directory -Force build | Out-Null

Write-Host "[1/4] Compiling Monitor.kt..."
kotlinc Monitor.kt -cp $androidJar -include-runtime -d build\monitor_raw.jar
if ($LASTEXITCODE -ne 0) { throw "kotlinc failed" }

Write-Host "[2/4] Converting to DEX jar (d8)..."
& $d8 --release --min-api 33 --lib $androidJar --output build\radar.jar build\monitor_raw.jar
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

Write-Host "[3/4] Pushing /data/local/tmp/radar.jar as root..."
adb root | Out-Null
adb wait-for-device
adb push build\radar.jar /data/local/tmp/radar.jar | Out-Null

if ($CheckDirs) {
    Write-Host "[4/4] Verifying watched directories (--check-dirs) on $Root ...`n"
    adb shell "CLASSPATH=/data/local/tmp/radar.jar app_process /system/bin MonitorKt --check-dirs --root $Root"
} elseif ($CheckEnv) {
    Write-Host "[4/4] Running Open Checks (--check-env)...`n"
    adb shell "CLASSPATH=/data/local/tmp/radar.jar app_process /system/bin MonitorKt --check-env"
} elseif ($SelfTest) {
    Write-Host "[4/4] Running End-to-End Self-Test (--selftest)...`n"
    adb shell "CLASSPATH=/data/local/tmp/radar.jar app_process /system/bin MonitorKt --selftest"
} else {
    Write-Host "[4/4] Starting live RadarMonitor on $Root...`n"
    adb shell "CLASSPATH=/data/local/tmp/radar.jar app_process /system/bin MonitorKt --root $Root --window-ms $WindowMs --min-files $MinFiles --min-score $MinScore --kill true"
}
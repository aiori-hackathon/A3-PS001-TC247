# Host-side tools

This directory contains the host-side tools for testing the Android Radar
monitor. The tools create a benign phone data set, build and launch the
monitor on an Android emulator, and pull the resulting telemetry into local
reports.

## Requirements

- A running Android emulator connected through `adb`
- Root access enabled for the emulator (`adb root`)
- Python 3
- PowerShell
- Kotlin compiler (`kotlinc`)
- Android SDK with `d8`, `android.jar`, and `adb`

Run the commands below from `src\host_side` unless noted otherwise.

## Quick start

1. Confirm that the emulator is available:

   ```powershell
   adb devices
   adb root
   ```

2. Generate the sample phone environment and push it to the emulator:

   ```powershell
   cd make_phone_env
   python make_phone_env.py --push
   ```

   The generated files are written locally to `make_phone_env\phone_env` and
   pushed to `/sdcard` by default. Use `--verify` to compare the device files
   with the generated manifest.

3. Build and start the monitor:

   ```powershell
   cd ..\monitor
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1
   ```

   The default monitor watches `/data/media/0` and writes device logs to
   `/data/local/tmp/radar_logs`.

4. Run the built-in monitor self-test instead of starting live monitoring:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -SelfTest
   ```

   Other useful checks are `-CheckEnv` and `-CheckDirs`.

5. Pull and format the logs:

   ```powershell
   cd ..\read_logs
   python radar_log_ui.py
   ```

   Reports are written to `read_logs\radar_reports`, including:

   - `formatted_logs.txt` - human-readable event and alert output
   - `formatted_logs.json` - structured log data
   - `metrics_summary.csv` - trial performance metrics
   - `raw_logs` - pulled monitor and ground-truth logs

## Log analysis options

Run a fresh non-destructive B1/A3 capture before generating reports:

```powershell
python radar_log_ui.py --demo
```

Analyze logs already present in `radar_reports\raw_logs` without pulling
anything from the emulator:

```powershell
python radar_log_ui.py --no-pull
```

The monitor supports additional tuning options such as `-Root`, `-WindowMs`,
`-MinFiles`, and `-MinScore`; run
`powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -?` for the
available build-script options.

# Android Radar Monitor: Behaviour-Based Detection Prototype

This directory contains a controlled-lab prototype for detecting ransomware-like
file activity on an Android emulator. The monitor observes filesystem events,
scores suspicious behaviour in a sliding window, attributes an alert to the
responsible process, and can stop that process. The workload scripts are
simulations only: they operate on generated lab data and do not encrypt or
modify a user's real device.

## Prototype capabilities

- **Behaviour-based detection:** combines bursts of writes, changes to baseline
  files, new-file creation, and suspicious extension changes such as
  `.txt -> .locked`.
- **Safe simulation scripts:** benign backup/edit workloads and attack-shaped
  overwrite/rename workloads run only in the emulator's generated dataset.
- **Event and telemetry logs:** JSONL filesystem events, alerts, process-kill
  records, health output, and ground-truth workload events.
- **Evaluation:** reports detection delay, kill latency, precision, recall,
  F1, files changed before mitigation, and benign processes killed.
- **Working demonstration:** the included `--demo` flow builds a fresh
  non-destructive B1/A3 capture and produces local reports.
- **Android instrumentation setup:** a Kotlin monitor is compiled to DEX and
  launched with Android's `app_process`; `FileObserver` watches the selected
  Android user-data tree.

## Safety boundaries

Run this prototype only on an emulator or disposable rooted test device.
`make_phone_env.py` generates synthetic Android-style content, and the workload
scripts verify the lab environment before changing files. The demonstration
uses `/data/media/0/radar_ui_sandbox`, removes that sandbox after capture, and
keeps the logs under `/data/local/tmp/radar_logs`.

Do not point the attack workloads at a personal phone, production device, or
directory containing real user data. The monitor's `--kill true` option is
intended for this controlled test only.

## Requirements

- Windows PowerShell
- Python 3 (the environment generator uses only the standard library; Pillow
  is optional)
- A running Android emulator connected through `adb`
- Root access enabled on the emulator (`adb root`)
- Kotlin compiler (`kotlinc`)
- Android SDK containing `android.jar`, `d8.bat`, and `adb`

Run the commands below from `src\host_side` unless noted otherwise.

## Quick start: working demonstration

1. Confirm the emulator and root access:

   ```powershell
   adb devices
   adb root
   ```

2. Generate and push the benign baseline dataset:

   ```powershell
   cd make_phone_env
   python make_phone_env.py --push
   python make_phone_env.py --verify
   ```

3. Build the Kotlin monitor and run its checks:

   ```powershell
   cd ..\monitor
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -SelfTest
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -CheckEnv
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -CheckDirs
   ```

4. Run a fresh B1 benign backup followed by an A3 attack-shaped trial, pull
   telemetry, and generate reports:

   ```powershell
   cd ..\read_logs
   python radar_log_ui.py --demo
   ```

   The command creates `read_logs\radar_reports`, including:

   - `formatted_logs.txt` - human-readable event, alert, and metric output
   - `formatted_logs.json` - structured events and calculated results
   - `metrics_summary.csv` - one row per evaluated trial
   - `raw_logs\events.jsonl` - normalized filesystem events
   - `raw_logs\alerts.jsonl` - detector alerts and scores
   - `raw_logs\kill.jsonl` - process attribution and mitigation records
   - `raw_logs\health.log` - monitor health/status output
   - `raw_logs\truth\*.jsonl` - ground-truth workload operations

5. To format logs already pulled into `radar_reports\raw_logs`:

   ```powershell
   python radar_log_ui.py --no-pull
   ```

## Detection model

`monitor\Monitor.kt` recursively watches the configured root with Android
`FileObserver`. It records raw events, normalizes rename/write activity, and
maintains a baseline of files present at startup. Actions are scored inside a
sliding time window:

- writes to baseline files;
- baseline-file renames;
- extension changes;
- creation of new files.

The default alert configuration is:

```text
root       /data/media/0
window     5000 ms
minimum    6 files and score 15.0
kill       enabled
```

Tune the detector when building or launching it:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 `
  -Root "/data/media/0" -WindowMs 5000 -MinFiles 6 -MinScore 15.0
```

The monitor writes device-side output to `/data/local/tmp/radar_logs` and
uses `/data/local/tmp/radar_registry` for workload registration and process
attribution.

## Workloads and test setup

The workload launcher supports:

| ID | Classification | Behaviour |
|---|---|---|
| `B1` | Benign | Copies photos into a backup directory |
| `B2` | Benign | Edits documents |
| `A1` | Attack-shaped | Overwrites files |
| `A2` | Attack-shaped | Encrypt/delete simulation |
| `A3` | Attack-shaped | Overwrites files, then renames them to `.locked` |
| `A3_slow` | Attack-shaped | Slower A3 variant |
| `E1`-`E4` | Edge cases | Slow-drip, temporary staging, micro-file burst, and detached worker patterns |

For repeated workload tests, first push the scripts and then use the harness:

```powershell
cd ..\workloads
powershell -ExecutionPolicy Bypass -File .\push_workloads.ps1
cd ..\harness
python launch_workload.py B1
python launch_workload.py A3
python launch_workload.py --all --trials 3
```

Each run resets the generated `/sdcard` dataset unless `--no-reset` is used.
The reset flag prevents the always-on monitor from treating test preparation as
an incident.

## Telemetry and evaluation

Ground truth is written by each workload as timestamped JSONL operations. The
reporting tool aligns those records with monitor events and alerts to calculate:

- **Detection delay:** first attack file operation to first detector alert.
- **Kill latency:** alert to attributed process termination.
- **Files changed:** distinct baseline files changed before the kill.
- **Precision:** `TP / (TP + FP)`.
- **Recall:** `TP / (TP + FN)`.
- **F1:** harmonic mean of precision and recall.
- **Benign kills:** benign trials whose process was terminated; the target is
  zero.

The checked-in demo snapshot in `read_logs\demo_radar_reports` contains one A3
attack trial and one B1 benign trial:

| Metric | Demo result |
|---|---:|
| Precision | 100.0% (TP=1, FP=0) |
| Recall | 100.0% (TP=1, FN=0) |
| Mean detection delay | 1174.116 ms |
| Mean kill latency | 844.686 ms |
| Mean total mitigation time | 2018.802 ms |
| Mean files changed before kill | 9/15 (60.0%) |
| Benign processes killed | 0 |

These are reproducible demonstration results, not a statistically significant
production benchmark. Run multiple trials with `--all --trials N` before making
comparisons between detector configurations.

## Android instrumentation details

The monitor is a Kotlin/JVM program compiled against the Android SDK's
`android.jar`. `build_monitor.ps1`:

1. compiles `Monitor.kt` to `monitor_raw.jar` with `kotlinc`;
2. converts the jar to a DEX-compatible `radar.jar` with `d8`;
3. pushes the jar to `/data/local/tmp/radar.jar`;
4. launches `MonitorKt` through:

   ```text
   CLASSPATH=/data/local/tmp/radar.jar
   app_process /system/bin MonitorKt
   ```

The test device must support the APIs used by the monitor and provide root
access for observing the selected tree and inspecting `/proc` process activity.
`-SelfTest`, `-CheckEnv`, and `-CheckDirs` validate the monitor and its watched
directories before a workload is run.

## Troubleshooting

- If `adb devices` is empty, start the emulator and reconnect `adb`.
- If `adb root` is rejected, use a rooted AOSP/Google APIs test image.
- If the build cannot find the SDK, set `ANDROID_HOME` or `ANDROID_SDK_ROOT`.
- If reports are empty, run `python radar_log_ui.py --demo` and confirm that
  `/data/local/tmp/radar_logs` contains `events.jsonl`.
- Use `build_monitor.ps1 -?` to see all build and detector options.

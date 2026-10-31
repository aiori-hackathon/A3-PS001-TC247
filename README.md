# Ransomware Radar
### AIORI-3 Hackathon | Problem Statement PS-001

**Ransomware Radar** is an Android prototype designed to identify suspicious, ransomware-like file activity within a controlled test environment. It monitors file-system events and applies predefined, rule-based thresholds to raise alerts during safe simulations.

## Overview

Ransomware can rapidly modify, rename, or delete files. Ransomware Radar explores how bursts of these file operations can provide an early warning signal on Android.

The application provides a dashboard for monitoring a dedicated lab directory, running safe file-activity simulations, and reviewing recorded events and alerts.

## Team

- **Team name:** Team Ransomware Radar
- **Project code:** A3-PS001-TC247
- **Team lead:** J Chandana
- **Team member:** K Teja

## Team Introduction Video
YouTube: https://www.youtube.com/watch?v=04T-SjmQzE4

## 🎥 Prototype Demo Video
YouTube: https://youtu.be/modn-y5I6U8


# Ransomware Radar: Behaviour-Based Detection in an Android Emulator Lab

Host-side prototype for **AIORI-3 PS-001** (team A3-PS001-TC247). It detects
ransomware-like file activity inside a controlled Android emulator: a Kotlin
monitor watches a designated test directory, scores bursts of rewrites, renames
and new files in a sliding window, attributes the burst to the responsible test
workload and terminates only that workload. All workloads are harmless
simulations that operate on generated dummy data.

## How this meets PS-001

| PS-001 requirement | Where it is met |
|---|---|
| One emulator or rooted lab device with documented telemetry | Rooted emulator via `adb`; telemetry format in [Telemetry and evaluation](#telemetry-and-evaluation) |
| Monitor an explicitly accessible test directory | `FileObserver` on `--root` (default `/data/media/0`, the `/sdcard` dummy dataset from `make_phone_env.py`) |
| 2 benign + 3 harmless ransomware-like workloads | `B1`, `B2` (benign); `A1`, `A2`, `A3` (attack-shaped). `A3_slow` and `E1`-`E4` are extras |
| Detection delay, files changed before alert, precision, recall | `read_logs/radar_log.py` -> `metrics_summary.csv` and `formatted_logs.*` |
| Detection from observable behaviour, not real malware | Rule-based scoring of file events only; no signatures, no real malware |
| Stop only the controlled test workload | Kill targets only workloads registered by `launcher.sh` ([details](#attribution-and-kill)) |
| No device-wide prevention claims, no monitoring of other apps | See [Scope and limitations](#scope-and-limitations) |
| Working demonstration | [Quick start](#quick-start) |
| Documentation of instrumentation and test setup | [Android instrumentation details](#android-instrumentation-details) |
| Resource cost of the detector | [Resource overhead](#resource-overhead) |

## Repository layout

```text
src/host_side/
  make_phone_env/   synthetic phone dataset generator + baseline.json manifest
  monitor/          Monitor.kt, start_daemon.ps1, build_monitor.ps1 (checks only), prebuilt jars
  workloads/        B1 B2 A1 A2 A3 E1-E4 shell workloads, lib.sh, launcher.sh, push_workloads.ps1
  harness/          launch_workload.py (run trials), measure_overhead.py (resource cost), radar_watch_ui.py (live view)
  read_logs/        radar_log.py (metrics + reports), radar_reports/ (checked-in reports)
```

## Safety boundaries

Run this only on an emulator or a disposable rooted test device. The dataset is
synthetic and the workload scripts verify the lab dataset exists before they
change anything. Do not point the attack workloads at a personal phone or at a
directory containing real data. `--kill true` is intended for this controlled
test only.

## Requirements

- Windows PowerShell
- Python 3 (standard library only; `rich` is needed for `harness/radar_watch_ui.py`, Pillow is optional)
- A running Android emulator reachable through `adb`, with root (`adb root`)
- Kotlin compiler (`kotlinc`)
- Android SDK with `android.jar`, `d8.bat` and `adb`

Run commands from `src\host_side` unless noted.

## Quick start

Order matters: the monitor records its **baseline at startup**, so the dataset
must be pushed before the daemon starts.

1. Confirm the emulator and root:

   ```powershell
   adb devices
   adb root
   ```

2. Generate and push the dummy dataset (100 files, seed 1337):

   ```powershell
   cd make_phone_env
   python make_phone_env.py --push
   python make_phone_env.py --verify
   ```

3. (Optional) pre-flight checks. They compile and push `radar.jar`, run a
   self-test on the device and exit. This is the only use of `build_monitor.ps1`;
   do not run it without a check flag, because that starts a second foreground
   monitor:

   ```powershell
   cd ..\monitor
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -SelfTest
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -CheckEnv
   powershell -ExecutionPolicy Bypass -File .\build_monitor.ps1 -CheckDirs
   ```

4. Build and start the monitor daemon. This one script compiles `Monitor.kt`,
   converts it to DEX, pushes `radar.jar`, and launches `MonitorKt` detached on
   the emulator:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start_daemon.ps1
   ```

   It ends with `MonitorKt daemon is RUNNING on emulator (PID: ...)`. Stop it
   with `.\start_daemon.ps1 -StopOnly`.

5. Push the workloads and run trials against the running daemon:

   ```powershell
   cd ..\workloads
   powershell -ExecutionPolicy Bypass -File .\push_workloads.ps1
   cd ..\harness
   python launch_workload.py --all --trials 3
   ```

   `--all` runs B1, B2, A1, A2, A3 and A3_slow. Each run first restores the
   dataset (see [Workloads](#workloads-and-test-setup)).

6. Pull the telemetry and build the reports:

   ```powershell
   cd ..\read_logs
   python radar_log.py
   ```

   Reports are written to `read_logs\radar_reports`:

   - `formatted_logs.txt`: human-readable events, alerts and metrics
   - `formatted_logs.json`: structured events and calculated results
   - `metrics_summary.csv`: one row per trial
   - `raw_logs\events.jsonl`, `alerts.jsonl`, `kill.jsonl`, `health.log`, `truth\*.jsonl`

   To reformat logs that were already pulled: `python radar_log.py --no-pull`.

7. (Optional) watch live in a second terminal while trials run:
   `python ..\harness\radar_watch_ui.py`.

### One-command demo (alternative flow)

```powershell
cd read_logs
python radar_log.py --demo
```

The demo is self-contained: it starts its own temporary monitor on a sandbox
folder (`/data/media/0/radar_ui_sandbox`), runs one benign B1 and one attack A3
trial, removes the sandbox and writes the same reports. It needs `radar.jar` on
the device (run `start_daemon.ps1` once first). **Stop the daemon before running
it**: the demo clears `/data/local/tmp/radar_logs` and ends with
`pkill -f MonitorKt`, which would kill the daemon and wipe its logs.

## Detection model

`monitor\Monitor.kt` recursively watches the configured root with Android
`FileObserver`, records raw events, normalizes rename/write activity and keeps a
baseline of files that existed at startup. Each action adds a weight to a score
inside a sliding time window:

- writes to baseline files;
- baseline-file renames;
- extension changes such as `.txt -> .locked`;
- creation of new files (including ransom-note style files).

An alert fires when both thresholds are met inside the window:

```text
root       /data/media/0
window     5000 ms
minimum    6 files and score 15.0
kill       enabled
```

Tune it when starting the daemon (same parameters as before):

```powershell
.\start_daemon.ps1 -Root "/data/media/0" -WindowMs 5000 -MinFiles 6 -MinScore 15.0
```

Device-side output is written to `/data/local/tmp/radar_logs`; workload
registration and attribution state live in `/data/local/tmp/radar_registry`.

### Attribution and kill

When an alert fires the monitor looks at the workloads registered by
`workloads\launcher.sh` (PID files in `/data/local/tmp/radar_registry`),
confirms each PID is alive and has the expected command line, samples
`/proc/<pid>/io` (write-byte delta) and open descriptors inside the watched
tree, and kills the most active one together with its in-flight child
commands. A process that is not registered is never a kill candidate: if no
registered workload is active the alert is still logged and the kill record
says `unattributed`. This is what limits the kill to the controlled test
workload.

## Workloads and test setup

| ID | Classification | Behaviour |
|---|---|---|
| `B1` | Benign | Copies up to 30 JPGs from `DCIM/Camera` into `Backups/<run id>/` |
| `B2` | Benign | Appends a line to up to 8 TXT/CSV files and saves each via an atomic `.tmp` rename |
| `A1` | Attack-shaped | Overwrites files in place with random bytes of the same size |
| `A2` | Attack-shaped | Writes a random-byte `.enc` copy of each file, then deletes the original |
| `A3` | Attack-shaped | Overwrites files, then renames them to `.locked`, then drops a ransom-note-style file |
| `A3_slow` | Attack-shaped | Slower A3 variant (0.5 s between files) |
| `E1`-`E4` | Edge cases | Slow drip, temp stage/delete, micro-file blitz, detached subshell worker |

Run a single workload with `python launch_workload.py B1`, or all core scenarios
with `--all --trials N`. Every run first sets a `.resetting` flag so the
always-on monitor ignores test preparation, restores the 100-file dataset with
`make_phone_env.py --push` (skip with `--no-reset`), then clears the flag.
Workloads write timestamped ground truth to `/data/local/tmp/radar_truth`.

## Telemetry and evaluation

Ground truth from the workloads is aligned with monitor events and alerts to
compute, per trial and overall:

- **Detection delay:** first attack file operation to first alert.
- **Files changed before alert:** distinct baseline files touched when the alert fired (`files_at_alert`).
- **Files changed before kill:** distinct baseline files touched until the process died (`files_changed`).
- **Kill latency:** alert to confirmed termination of the attributed process.
- **Precision** `TP / (TP + FP)`, **recall** `TP / (TP + FN)` and **F1**.
- **Benign kills:** benign trials whose workload was terminated; the target is zero.

| Log | Content |
|---|---|
| `events.jsonl` | Normalized filesystem events (`ts_ns`, path, event type, baseline flag) |
| `alerts.jsonl` | Alert time, score, window, reasons and attribution candidates |
| `kill.jsonl` | Killed PID/name, result (`ok`, `unattributed`) and timestamp |
| `health.log` | Monitor start-up line (root, baseline size, watchers, thresholds) and watched directories |
| `truth\*.jsonl` | Ground-truth operations written by each workload |

### Results

The checked-in reports in `read_logs\radar_reports` include an A3 attack trial
and a B1 benign trial:

| Metric | Sample result |
|---|---:|
| Precision | 100.0% (TP=1, FP=0) |
| Recall | 100.0% (TP=1, FN=0) |
| Mean detection delay | 1174.116 ms |
| Mean kill latency | 844.686 ms |
| Mean total mitigation time | 2018.802 ms |
| Mean files changed before alert | 6/15 (40.0%) |
| Mean files changed before kill | 9/15 (60.0%) |
| Benign processes killed | 0 |

This is a demonstration of the pipeline, not a benchmark. For comparisons run
`python launch_workload.py --all --trials 3` (or more) and report the resulting
`metrics_summary.csv`.

## Resource overhead

`harness\measure_overhead.py` measures what the monitor itself costs on the
device. It reads `/proc` as root and needs no extra tools.

```powershell
cd harness
python measure_overhead.py idle --duration 60        # steady-state cost, daemon running, no workload
python measure_overhead.py load --trials 3           # cost while B1 B2 A1 A2 A3 run
python measure_overhead.py slowdown --trials 5       # benign B1/B2 run time, monitor OFF vs ON
python measure_overhead.py all --trials 3            # all three, one combined report
```

`idle` and `load` need the daemon running (`start_daemon.ps1`). `slowdown` stops
and restarts the monitor itself and leaves it running at the end.

| Measure | How |
|---|---|
| CPU of the monitor | `utime+stime` ticks of its PID over total device ticks in the same interval, reported as % of one core and % of the device (mean, p95, max) |
| Memory | `VmRSS` (current), `VmHWM` (peak) and `Pss` when `smaps_rollup` exists |
| Threads, file descriptors, inotify watches | `/proc/<pid>/status`, `/fd`, `/fdinfo` |
| System context | whole-device busy % and `MemAvailable` |
| Log storage | bytes written to `/data/local/tmp/radar_logs` and bytes per event |
| Workload slowdown | wall time of the same benign workload with the monitor off vs on, measured on the device and alternated per trial |

Output goes to `harness\overhead_reports`: `overhead_samples.csv` (one row per
sample), `overhead_summary.json` and `overhead_summary.md` (tables ready to paste
into the report). Notes for quoting results:

- Emulator numbers depend on the host PC. Quote the host CPU and emulator image with them.
- Only benign workloads are used for the slowdown comparison, because attack workloads are killed by design.
- Battery drain cannot be measured on an emulator. On a rooted physical phone, run
  `adb shell dumpsys batterystats --reset`, run the same workloads with and without the daemon, then read
  `adb shell dumpsys batterystats` and look at the UID 0 (root) entry. Treat the result as indicative only.

## Android instrumentation details

The monitor is a Kotlin/JVM program compiled against the Android SDK's
`android.jar`. `start_daemon.ps1`:

1. compiles `Monitor.kt` to `monitor_raw.jar` with `kotlinc`, then converts it to a DEX-compatible `radar.jar` with `d8`;
2. pushes the jar to `/data/local/tmp/radar.jar` and creates `/data/local/tmp/radar_logs`, `radar_registry` and `radar_truth`;
3. launches it detached on the device:

   ```text
   CLASSPATH=/data/local/tmp/radar.jar
   app_process /system/bin MonitorKt --root <root> --window-ms <n> --min-files <n> --min-score <x> --kill true
   ```

The test device needs root to observe the selected tree and read `/proc` for
attribution. `FileObserver` is inotify-based, so it registers one watch per
directory; the number of watches is reported by `measure_overhead.py`.
`radar.jar` and `monitor_raw.jar` are checked in under `monitor\build` so the
demo can run without a rebuild, but `start_daemon.ps1` always rebuilds them.

## Scope and limitations

- Runs only in a controlled lab: an emulator or disposable rooted device. It is not a product for stock, unrooted Android and makes no device-wide prevention claim.
- It watches one directory tree and does not inspect other apps' memory or behaviour.
- Attribution relies on workload registration. An unregistered process raises an alert but is not killed.
- Detection is rule-based (thresholds and weights), tuned on synthetic data. It is not machine learning and has not been tested against real malware.
- `FileObserver` is inotify-based and can drop events if its queue overflows under extreme write rates.
- Evaluation uses few trials of synthetic workloads; treat percentages accordingly.

## Troubleshooting

- `adb devices` is empty: start the emulator and reconnect `adb`.
- `adb root` is rejected: use a rooted AOSP / Google APIs test image.
- The build cannot find the SDK: set `ANDROID_HOME` or `ANDROID_SDK_ROOT`.
- Reports are empty: confirm the daemon is running (`python measure_overhead.py idle --duration 5` finds the monitor by name and fails if it is not) and that `/data/local/tmp/radar_logs/events.jsonl` exists.
- `--demo` shows no events or the daemon died: the demo stops any running monitor by design; restart it with `start_daemon.ps1` afterwards.
- Trials start from changed files: the harness restores the dataset before each run; if `make_phone_env.py --push` fails the run now stops with the error instead of continuing.
- Parameters: `Get-Help .\start_daemon.ps1`.

# Ransomware Radar
### AIORI-3 Hackathon | Problem Statement PS-001

**Ransomware Radar** is an Android prototype designed to identify suspicious, ransomware-like file activity within a controlled test environment. It monitors file-system events and applies predefined, rule-based thresholds to raise alerts during safe simulations.

> **Safety note:** This project is designed for controlled lab testing with dummy files. It is an early-warning prototype, not a production antivirus or a guarantee that ransomware will be stopped.

## Overview

Ransomware can rapidly modify, rename, or delete files. Ransomware Radar explores how bursts of these file operations can provide an early warning signal on Android.

The application provides a dashboard for monitoring a dedicated lab directory, running safe file-activity simulations, and reviewing recorded events and alerts.

## Key Features

- **File-event monitoring:** Uses Android's `FileObserver` to observe activity in the configured lab directory.
- **Rule-based detection:** Evaluates recent file activity against predefined thresholds. No machine-learning model is used.
- **Safe simulation:** Generates controlled file operations on dummy test files.
- **Event and alert logs:** Records observed events and generated alerts for inspection.
- **Monitoring health log:** Records monitor-health information.
- **Move-event analysis:** Records move-related events such as `MOVED_FROM` and `MOVED_TO`.

## How It Works

1. Start monitoring the `ransomware_radar_lab` test directory.
2. Run the safe simulation to generate controlled file activity.
3. The monitor observes file-system events.
4. The detection logic evaluates recent activity against configured rules.
5. When a rule is met, the application can raise an alert and record relevant information in its logs.

An alert indicates suspicious activity according to the configured rules; it does not prove that a real ransomware attack has occurred.

## Technology Stack

- **Platform:** Android
- **Programming language:** Kotlin
- **User interface:** Jetpack Compose
- **File monitoring:** Android `FileObserver`
- **Detection method:** Rule-based, time-window event analysis
- **Build system:** Gradle

## Getting Started

1. Clone or download this repository.
2. Open the Android project in Android Studio. The project is located in [`src/sourceCode`](src/sourceCode/).
3. Allow Android Studio to complete the Gradle sync.
4. Run the application on a supported Android emulator or test device.
5. Use the monitoring and simulation controls to test activity inside the designated lab directory.

**Use only disposable dummy files for testing.** Do not test against personal data or files you do not have permission to modify.

## Logs

The application stores monitoring logs in its app-specific files area under `ransomware_radar_logs`.

| Log file | Purpose |
|---|---|
| `events.jsonl` | Records observed file events |
| `alerts.jsonl` | Records generated alerts |
| `monitor_health.log` | Records monitoring-health information |

The exact location of these files may depend on the device, emulator, and app version.

## Scope and Limitations

- This is a hackathon prototype intended for controlled testing.
- Detection is based on predefined rules, not machine learning.
- File-event visibility can vary across Android versions and storage locations.
- The prototype does not claim comprehensive ransomware detection or guaranteed automatic containment.
- Testing should remain restricted to the designated lab directory and disposable dummy files.

## Team

- **Team name:** Team Ransome Radar
- **Project code:** A3-PS001-TC247
- **Team lead:** J Chandana

## Team Introduction Video
YouTube: https://www.youtube.com/watch?v=04T-SjmQzE4

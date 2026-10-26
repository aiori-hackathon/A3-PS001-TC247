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

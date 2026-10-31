#!/usr/bin/env python3
"""
radar_watch.py - Window 1 Live Terminal UI & Incident/Metrics Reporter (uses `rich`).
Passively tails /data/local/tmp/radar_logs/ on the emulator without MonitorKt
connecting to the host. Automatically generates a full timeline & PS-001 metrics
report whenever an attack is detected or a workload finishes.
"""
import argparse, csv, datetime, json, os, queue, subprocess, threading, time
from collections import deque
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

def fmt_ns(ts_ns: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ns / 1e9)
    return dt.strftime("%H:%M:%S.") + f"{int((ts_ns % 1_000_000_000) / 1_000_000):03d}"

def adb_shell(cmd: str) -> str:
    r = subprocess.run(["adb", "shell", cmd], capture_output=True, text=True, errors="replace")
    return r.stdout.strip()

def get_latest_truth_log():
    out = adb_shell("ls -t /data/local/tmp/radar_truth/*.jsonl 2>/dev/null | head -n 1")
    if not out or "No such file" in out:
        return None, []
    raw = adb_shell(f"cat '{out}'")
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return out, records

def count_corrupted_baseline_files(baseline_map: dict, device_root: str = "/sdcard") -> int:
    """Fast on-device check of how many baseline files are missing or changed."""
    if not baseline_map:
        return 0
    # Pull current file list and sizes/existence, plus check sha256 on existing baseline paths
    script_lines = ["changed=0"]
    for rel_path, info in baseline_map.items():
        dev_p = f"{device_root}/{rel_path}"
        expected_sha = info["sha256"]
        script_lines.append(
            f'if [ ! -f "{dev_p}" ]; then changed=$((changed+1)); '
            f'else h=$(sha256sum "{dev_p}" 2>/dev/null | cut -d" " -f1); '
            f'[ "$h" != "{expected_sha}" ] && changed=$((changed+1)); fi'
        )
    script_lines.append('echo "$changed"')
    r = subprocess.run(["adb", "shell", "\n".join(script_lines)], capture_output=True, text=True)
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return -1

class MetricsTracker:
    def __init__(self, csv_path: Path, baseline_path: Path):
        self.csv_path = csv_path
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.trials = []
        self.seen_runs = set()
        self.baseline_map = {}
        if baseline_path.exists():
            data = json.loads(baseline_path.read_text())
            self.baseline_map = {f["path"]: f for f in data.get("files", [])}

        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "trial", "run_id", "scenario", "label", "alerted",
                    "delay_ms", "files_changed", "killed_pid", "killed_name", "benign_killed"
                ])

    def record_and_render_report(self, alert_obj, kill_obj, recent_events, truth_records):
        trial_num = len(self.trials) + 1

        # Extract truth metadata if available
        run_id = truth_records[0].get("run_id", f"manual_{trial_num}") if truth_records else f"manual_{trial_num}"
        workload = truth_records[0].get("workload", kill_obj.get("name", "UNKNOWN") if kill_obj else "UNKNOWN") if truth_records else (kill_obj.get("name", "UNKNOWN") if kill_obj else "UNKNOWN")
        label = truth_records[0].get("label", "attack" if alert_obj else "benign") if truth_records else ("attack" if alert_obj else "benign")

        self.seen_runs.add(run_id)

        # Find first file operation timestamp (from truth log if present, else first baseline event in window)
        op_records = [r for r in truth_records if r.get("op") not in ("start", "end", "skip")]
        first_op_ns = op_records[0]["ts_ns"] if op_records else (recent_events[0]["ts_ns"] if recent_events else (alert_obj["ts_ns"] if alert_obj else 0))
        first_op_path = op_records[0].get("path", recent_events[0].get("path", "?")) if (op_records or recent_events) else "?"

        alerted = alert_obj is not None
        alert_ns = alert_obj["ts_ns"] if alert_obj else 0
        kill_ns = kill_obj["ts_ns"] if kill_obj else 0

        delay_ms = round((alert_ns - first_op_ns) / 1e6, 2) if (alerted and first_op_ns > 0) else -1.0
        kill_delay_ms = round((kill_ns - first_op_ns) / 1e6, 2) if (kill_obj and first_op_ns > 0) else -1.0

        # Count files changed in truth log or via baseline verification
        if label == "benign":
            files_changed = 0
        else:
            # Distinct baseline files modified/overwritten/deleted in truth log before kill
            touched_in_truth = {
                r.get("path") for r in op_records
                if r.get("op") in ("overwrite", "modify", "delete", "rename") and r.get("path")
            }
            files_changed = len(touched_in_truth) if touched_in_truth else (alert_obj.get("files_touched", 0) if alert_obj else 0)

        killed_pid = kill_obj.get("pid", -1) if kill_obj else -1
        killed_name = kill_obj.get("name", "none") if kill_obj else "none"
        benign_killed = (label == "benign" and killed_pid > 0)

        row = {
            "trial": trial_num,
            "run_id": run_id,
            "scenario": workload,
            "label": label,
            "alerted": alerted,
            "delay_ms": delay_ms,
            "files_changed": files_changed,
            "killed_pid": killed_pid,
            "killed_name": killed_name,
            "benign_killed": benign_killed
        }
        self.trials.append(row)
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row.values())

        # --- 1. Render Incident & Subsequent Logs Timeline Panel ---
        timeline_table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan", expand=True)
        timeline_table.add_column("Stage / Event", style="bold")
        timeline_table.add_column("Clock Time", width=14)
        timeline_table.add_column("Delta (ms)", justify="right", width=12)
        timeline_table.add_column("Details / Path")

        if first_op_ns > 0:
            timeline_table.add_row(
                "[yellow]1st Activity[/yellow]",
                fmt_ns(first_op_ns),
                "0.00 ms",
                f"Workload={workload} ({label}) | First target: {first_op_path}"
            )

        # Show subsequent observed file events leading up to the alert
        sub_events = [e for e in recent_events if e["ts_ns"] >= first_op_ns - 50_000_000][-12:]
        for idx, ev in enumerate(sub_events, 1):
            delta = (ev["ts_ns"] - first_op_ns) / 1e6 if first_op_ns else 0.0
            b_tag = "[bold red][BASELINE][/bold red]" if ev.get("baseline") else "[dim][NEW][/dim]"
            timeline_table.add_row(
                f"Event #{idx} ({ev['type']})",
                fmt_ns(ev["ts_ns"]),
                f"+{delta:.2f} ms",
                f"{b_tag} {ev['path']}"
            )

        if alert_obj:
            timeline_table.add_row(
                "[bold red]ALERT FIRED[/bold red]",
                fmt_ns(alert_ns),
                f"[bold red]+{delay_ms:.2f} ms[/bold red]",
                f"Score={alert_obj.get('score')} | Baseline Files Touched={alert_obj.get('files_touched')} | Reasons={','.join(alert_obj.get('reasons', []))}"
            )

        if kill_obj:
            k_color = "bold green" if kill_obj.get("result") == "ok" else "bold yellow"
            timeline_table.add_row(
                f"[{k_color}]PROCESS KILLED[/{k_color}]",
                fmt_ns(kill_ns),
                f"[{k_color}]+{kill_delay_ms:.2f} ms[/{k_color}]",
                f"Target={killed_name} (PID {killed_pid}) | Result={kill_obj.get('result')} | Files Changed Before Stop={files_changed}/100"
            )

        title_color = "red" if alerted else "green"
        status_title = f"RANSOMWARE ALERT & KILL REPORT — Trial #{trial_num} ({workload})" if alerted else f"BENIGN WORKLOAD COMPLETED CLEANLY — Trial #{trial_num} ({workload})"
        console.print()
        console.print(Panel(timeline_table, title=f"[bold {title_color}]{status_title}[/bold {title_color}]", border_style=title_color))

        # --- 2. Render Cumulative PS-001 Evaluation Metrics Report ---
        tp = sum(1 for t in self.trials if t["label"] == "attack" and t["alerted"])
        fn = sum(1 for t in self.trials if t["label"] == "attack" and not t["alerted"])
        fp = sum(1 for t in self.trials if t["label"] == "benign" and t["alerted"])
        tn = sum(1 for t in self.trials if t["label"] == "benign" and not t["alerted"])

        precision = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
        recall = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0
        delays = [t["delay_ms"] for t in self.trials if t["label"] == "attack" and t["alerted"] and t["delay_ms"] >= 0]
        avg_delay = (sum(delays) / len(delays)) if delays else 0.0
        changed_list = [t["files_changed"] for t in self.trials if t["label"] == "attack" and t["alerted"]]
        avg_changed = (sum(changed_list) / len(changed_list)) if changed_list else 0.0
        any_benign_killed = any(t["benign_killed"] for t in self.trials)

        metrics_table = Table(title="PS-001 Cumulative Evaluation Metrics (Saved to results/trials.csv)", box=box.ROUNDED, header_style="bold magenta", expand=True)
        metrics_table.add_column("Total Trials", justify="center")
        metrics_table.add_column("TP / FN / FP / TN", justify="center")
        metrics_table.add_column("Precision", justify="center")
        metrics_table.add_column("Recall", justify="center")
        metrics_table.add_column("Mean Alert Delay", justify="center")
        metrics_table.add_column("Mean Files Changed", justify="center")
        metrics_table.add_column("Benign Killed?", justify="center")

        metrics_table.add_row(
            str(len(self.trials)),
            f"[green]TP={tp}[/green]  [red]FN={fn}[/red]  [yellow]FP={fp}[/yellow]  [cyan]TN={tn}[/cyan]",
            f"[bold]{precision*100:.1f}%[/bold]",
            f"[bold]{recall*100:.1f}%[/bold]",
            f"{avg_delay:.2f} ms",
            f"{avg_changed:.1f} / 100",
            "[bold red]YES (FAIL)[/bold red]" if any_benign_killed else "[bold green]0 (PASS)[/bold green]"
        )
        console.print(metrics_table)
        console.print()

def stream_file(remote_path: str, tag: str, out_q: queue.Queue):
    cmd = ["adb", "shell", f"tail -n 0 -F '{remote_path}' 2>/dev/null"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        line = line.strip()
        if line.startswith("{"):
            out_q.put((tag, line))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="results/trials.csv")
    ap.add_argument("--baseline", default="phone_env/baseline.json")
    args = ap.parse_args()

    tracker = MetricsTracker(Path(args.csv), Path(args.baseline))
    q = queue.Queue()

    # Tail events.jsonl and alerts.jsonl passively from the emulator
    threading.Thread(target=stream_file, args=("/data/local/tmp/radar_logs/events.jsonl", "events", q), daemon=True).start()
    threading.Thread(target=stream_file, args=("/data/local/tmp/radar_logs/alerts.jsonl", "alerts", q), daemon=True).start()

    console.print(Panel.fit(
        "[bold green]PS-001 Live Telemetry Stream & Incident Reporter Active[/bold green]\n"
        "• Passively tailing [cyan]/data/local/tmp/radar_logs/[/cyan] on Android emulator\n"
        "• Launch attacks or benign workloads in [bold yellow]Window 2[/bold yellow] — reports & metrics will appear here automatically.",
        border_style="green"
    ))

    recent_events = deque(maxlen=60)
    pending_alert = None
    pending_alert_time = 0.0
    last_benign_check = time.time()

    # Pre-populate already existing truth runs so we don't report old history at startup
    _, init_recs = get_latest_truth_log()
    if init_recs:
        tracker.seen_runs.add(init_recs[0].get("run_id", ""))

    while True:
        try:
            tag, line = q.get(timeout=0.25)
            obj = json.loads(line)
            kind = obj.get("kind")

            if tag == "events" and kind == "event":
                recent_events.append(obj)
                ev_type = obj.get("type", "?")
                is_base = obj.get("baseline", False)
                color = "yellow" if ev_type in ("MODIFY", "CLOSE_WRITE") else ("red" if ev_type in ("DELETE", "MOVED_FROM", "MOVED_TO") else "cyan")
                base_badge = "[bold red]BASE[/bold red]" if is_base else "[dim]NEW [/dim]"
                console.print(f"[dim]{fmt_ns(obj['ts_ns'])}[/dim]  {base_badge}  [{color}]{ev_type:<11}[/{color}]  {obj.get('path')}")

            elif tag == "alerts":
                if kind == "alert":
                    pending_alert = obj
                    pending_alert_time = time.time()
                    console.print(f"\n[bold white on red] >>> ALERT DETECTED: Score={obj.get('score')} | Files Touched={obj.get('files_touched')} | Waiting for Kill confirmation... [/bold white on red]")
                elif kind == "kill" and pending_alert is not None:
                    time.sleep(0.15)  # allow truth log flush
                    _, truth_recs = get_latest_truth_log()
                    tracker.record_and_render_report(pending_alert, obj, list(recent_events), truth_recs)
                    pending_alert = None

        except queue.Empty:
            pass

        # If an alert fired with --kill false (no kill record arrives within 0.6s)
        if pending_alert is not None and (time.time() - pending_alert_time) > 0.6:
            _, truth_recs = get_latest_truth_log()
            tracker.record_and_render_report(pending_alert, None, list(recent_events), truth_recs)
            pending_alert = None

        # Periodically check if a benign workload (or undetected E1 attack) finished without triggering an alert
        if pending_alert is None and (time.time() - last_benign_check) > 1.5:
            last_benign_check = time.time()
            _, truth_recs = get_latest_truth_log()
            if truth_recs:
                run_id = truth_recs[0].get("run_id", "")
                has_end = any(r.get("op") == "end" for r in truth_recs)
                if run_id and has_end and run_id not in tracker.seen_runs:
                    tracker.record_and_render_report(None, None, list(recent_events), truth_recs)

if __name__ == "__main__":
    main()
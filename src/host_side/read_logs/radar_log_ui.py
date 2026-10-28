#!/usr/bin/env python3
"""
radar_log_ui.py - Pulls RadarMonitor logs from the Android emulator, formats them
into clean .txt and .json files on the laptop, and computes/prints PS-001 performance metrics.

Usage:
  python radar_log_ui.py            # Pulls logs (or runs a quick B1+A3 capture if empty) & prints metrics
  python radar_log_ui.py --demo     # Forces a fresh non-destructive B1 + A3 capture on the emulator
  python radar_log_ui.py --no-pull  # Runs metrics on already-pulled logs in ./radar_reports/raw_logs
"""

import argparse
import collections
import csv
import datetime
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# ADB Helpers & Clean Demo Workload Runner
# ---------------------------------------------------------------------------
def run_adb(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def device_logs_exist(device_log_dir: str) -> bool:
    res = run_adb("shell", f"test -s '{device_log_dir}/events.jsonl' && echo YES || echo NO")
    return "YES" in res.stdout


def write_lf_script(local_path: Path, content: str) -> None:
    """Writes a POSIX shell script with strict LF (\\n) line endings for Android sh."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content.replace("\r\n", "\n").encode("utf-8"))


def run_live_demo_trial_on_device(
    device_log_dir: str,
    device_reg_dir: str,
    device_truth_dir: str,
    local_tmp_dir: Path,
) -> None:
    """
    Runs a real non-destructive trial (Benign B1 backup + Attack A3 ransomware simulation)
    inside /data/media/0/radar_ui_sandbox using /data/local/tmp/radar.jar so real logs exist.
    """
    print("[*] Running a live non-destructive B1 + A3 capture on the emulator to populate logs...")
    run_adb("root")
    sandbox = "/data/media/0/radar_ui_sandbox"

    setup_cmd = (
        f"rm -rf {sandbox} {device_log_dir} {device_reg_dir} {device_truth_dir}; "
        f"mkdir -p {sandbox}/Documents {sandbox}/DCIM/Camera {device_log_dir} {device_reg_dir} {device_truth_dir}; "
        f"for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do "
        f"  printf 'Baseline document content for file %d\\n' $i > {sandbox}/Documents/note_$i.txt; "
        f"done"
    )
    run_adb("shell", setup_cmd)

    # Write B1 (benign) and A3 (attack) scripts locally with LF line endings and push them
    b1_sh = f"""#!/system/bin/sh
mkdir -p {sandbox}/Backups/run_b1
t0=$(date +%s%N)
printf '{{"ts_ns":%s,"run_id":"run_b1","workload":"B1","label":"benign","op":"start"}}\\n' "$t0" >> {device_truth_dir}/run_b1.jsonl
for i in 1 2 3 4 5 6 7 8 9 10; do
    echo "backup_image_data_$i" > "{sandbox}/Backups/run_b1/IMG_00$i.jpg"
    ts=$(date +%s%N)
    printf '{{"ts_ns":%s,"run_id":"run_b1","workload":"B1","label":"benign","op":"copy","path":"{sandbox}/Backups/run_b1/IMG_00%d.jpg"}}\\n' "$ts" "$i" >> {device_truth_dir}/run_b1.jsonl
    sleep 0.05
done
t1=$(date +%s%N)
printf '{{"ts_ns":%s,"run_id":"run_b1","workload":"B1","label":"benign","op":"end"}}\\n' "$t1" >> {device_truth_dir}/run_b1.jsonl
"""

    a3_sh = f"""#!/system/bin/sh
t0=$(date +%s%N)
printf '{{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"start"}}\\n' "$t0" >> {device_truth_dir}/run_a3.jsonl
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    f="{sandbox}/Documents/note_$i.txt"
    head -c 512 /dev/urandom > "$f"
    ts1=$(date +%s%N)
    printf '{{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"overwrite","path":"%s"}}\\n' "$ts1" "$f" >> {device_truth_dir}/run_a3.jsonl
    mv "$f" "$f.locked"
    ts2=$(date +%s%N)
    printf '{{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"rename","path":"%s","new_path":"%s.locked"}}\\n' "$ts2" "$f" "$f" >> {device_truth_dir}/run_a3.jsonl
    sleep 0.1
done
t1=$(date +%s%N)
printf '{{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"end"}}\\n' "$t1" >> {device_truth_dir}/run_a3.jsonl
"""

    b1_local = local_tmp_dir / "b1_sim.sh"
    a3_local = local_tmp_dir / "a3_sim.sh"
    write_lf_script(b1_local, b1_sh)
    write_lf_script(a3_local, a3_sh)

    run_adb("push", str(b1_local), f"{device_reg_dir}/b1_sim.sh")
    run_adb("push", str(a3_local), f"{device_reg_dir}/a3_sim.sh")
    run_adb("shell", f"chmod +x {device_reg_dir}/b1_sim.sh {device_reg_dir}/a3_sim.sh")

    # Start MonitorKt on device
    mon_proc = subprocess.Popen(
        [
            "adb",
            "shell",
            f"CLASSPATH=/data/local/tmp/radar.jar app_process /system/bin MonitorKt "
            f"--root {sandbox} --window-ms 5000 --min-files 6 --min-score 15.0 "
            f"--kill true --registry {device_reg_dir} --out {device_log_dir}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    time.sleep(0.8)

    # 1. Run Benign B1
    run_adb(
        "shell",
        f"sh {device_reg_dir}/b1_sim.sh & pid=$!; echo $pid > {device_reg_dir}/B1.pid; wait $pid; rm -f {device_reg_dir}/B1.pid",
        timeout=10,
    )
    time.sleep(0.5)

    # 2. Run Attack A3
    run_adb(
        "shell",
        f"sh {device_reg_dir}/a3_sim.sh & pid=$!; echo $pid > {device_reg_dir}/A3.pid; wait $pid",
        timeout=10,
    )
    time.sleep(0.5)

    # Stop MonitorKt and remove sandbox directory (keep logs in /data/local/tmp/radar_logs)
    run_adb("shell", "pkill -f MonitorKt || true")
    try:
        mon_proc.terminate()
    except Exception:
        pass
    run_adb("shell", f"rm -rf {sandbox}")
    print("[+] Live capture complete. Logs stored in /data/local/tmp/radar_logs.")


def pull_logs_from_device(
    device_log_dir: str,
    device_truth_dir: str,
    local_raw_dir: Path,
) -> None:
    local_raw_dir.mkdir(parents=True, exist_ok=True)
    truth_local = local_raw_dir / "truth"
    truth_local.mkdir(parents=True, exist_ok=True)

    for fname in ("events.jsonl", "alerts.jsonl", "kill.jsonl", "health.log"):
        run_adb("pull", f"{device_log_dir}/{fname}", str(local_raw_dir / fname))

    res = run_adb("shell", f"ls {device_truth_dir}/*.jsonl 2>/dev/null || true")
    for line in res.stdout.splitlines():
        remote_path = line.strip()
        if remote_path.endswith(".jsonl"):
            name = Path(remote_path).name
            run_adb("pull", remote_path, str(truth_local / name))


# ---------------------------------------------------------------------------
# Log Parsing & Performance Calculations
# ---------------------------------------------------------------------------
def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def ns_to_time_str(ts_ns: int) -> str:
    if ts_ns <= 0:
        return "-"
    dt = datetime.datetime.fromtimestamp(ts_ns / 1e9)
    ms = int((ts_ns % 1_000_000_000) / 1_000_000)
    us = int((ts_ns % 1_000_000) / 1_000)
    return dt.strftime("%H:%M:%S") + f".{ms:03d}.{us:03d}"


def compute_trial_metrics(
    trial_num: int,
    scenario: str,
    label: str,
    seg_events: List[Dict[str, Any]],
    seg_alerts: List[Dict[str, Any]],
    seg_kills: List[Dict[str, Any]],
    truth_first_op_ns: Optional[int],
    truth_has_end: Optional[bool],
    baseline_total: int,
) -> Dict[str, Any]:
    alerted = len(seg_alerts) > 0
    first_alert = seg_alerts[0] if alerted else None
    first_kill = seg_kills[0] if seg_kills else None

    base_events = [e for e in seg_events if e.get("baseline")]
    first_base_ns = base_events[0]["ts_ns"] if base_events else (seg_events[0]["ts_ns"] if seg_events else 0)
    start_ref_ns = truth_first_op_ns if truth_first_op_ns else first_base_ns

    alert_ts_ns = first_alert["ts_ns"] if first_alert else 0
    kill_ts_ns = first_kill["ts_ns"] if first_kill else 0

    delay_ms = max(0.0, (alert_ts_ns - start_ref_ns) / 1e6) if (alerted and start_ref_ns) else 0.0
    telemetry_delay_ms = max(0.0, (alert_ts_ns - first_base_ns) / 1e6) if (alerted and first_base_ns) else 0.0
    kill_latency_ms = max(0.0, (kill_ts_ns - alert_ts_ns) / 1e6) if (alerted and kill_ts_ns > 0) else 0.0
    total_mitigation_ms = (
        max(0.0, (kill_ts_ns - start_ref_ns) / 1e6)
        if (alerted and kill_ts_ns > 0 and start_ref_ns)
        else delay_ms
    )

    distinct_base_paths: List[str] = []
    file_first_touch_ns: Dict[str, int] = {}
    for e in seg_events:
        if e.get("baseline"):
            p = e["path"]
            if p not in file_first_touch_ns:
                file_first_touch_ns[p] = e["ts_ns"]
                distinct_base_paths.append(p)

    files_at_alert = (
        first_alert.get("files_touched", len(distinct_base_paths))
        if first_alert
        else len(distinct_base_paths)
    )
    files_changed_total = len(distinct_base_paths)
    files_saved = max(0, baseline_total - files_changed_total)
    survival_pct = round((files_saved / max(1, baseline_total)) * 100.0, 1)

    touch_times = sorted(file_first_touch_ns.values())
    if len(touch_times) >= 2:
        deltas = [(touch_times[i] - touch_times[i - 1]) / 1e6 for i in range(1, len(touch_times))]
        pace_ms_per_file = round(sum(deltas) / len(deltas), 2)
    else:
        pace_ms_per_file = 0.0

    killed_pid = first_kill.get("pid", -1) if first_kill else -1
    killed_name = first_kill.get("name", "none") if first_kill else "none"
    kill_result = first_kill.get("result", "none") if first_kill else "none"
    benign_killed = (label == "benign") and (kill_result == "ok")

    if label == "attack":
        outcome = "TP" if alerted else "FN"
    else:
        outcome = "FP" if alerted else "TN"

    return {
        "trial": trial_num,
        "scenario": scenario,
        "label": label,
        "outcome": outcome,
        "alerted": alerted,
        "score": first_alert.get("score", 0.0) if first_alert else 0.0,
        "reasons": first_alert.get("reasons", []) if first_alert else [],
        "delay_ms": round(delay_ms, 3),
        "telemetry_delay_ms": round(telemetry_delay_ms, 3),
        "kill_latency_ms": round(kill_latency_ms, 3),
        "total_mitigation_ms": round(total_mitigation_ms, 3),
        "files_at_alert": files_at_alert,
        "files_changed": files_changed_total,
        "files_saved": files_saved,
        "survival_pct": survival_pct,
        "pace_ms_per_file": pace_ms_per_file,
        "events_count": len(seg_events),
        "killed_pid": killed_pid,
        "killed_name": killed_name,
        "kill_result": kill_result,
        "benign_killed": benign_killed,
        "stopped_before_completion": (truth_has_end is False),
        "first_op_time": ns_to_time_str(start_ref_ns),
        "alert_time": ns_to_time_str(alert_ts_ns),
        "kill_time": ns_to_time_str(kill_ts_ns),
        "touched_files_list": [Path(p).name for p in distinct_base_paths],
    }


def analyze_logs(local_raw_dir: Path, baseline_json_path: Optional[Path]) -> Dict[str, Any]:
    events = [e for e in load_jsonl(local_raw_dir / "events.jsonl") if e.get("kind") == "event"]
    alerts_raw = load_jsonl(local_raw_dir / "alerts.jsonl")
    alerts = [a for a in alerts_raw if a.get("kind") == "alert"]
    kills = load_jsonl(local_raw_dir / "kill.jsonl")
    if not kills:
        kills = [a for a in alerts_raw if a.get("kind") == "kill"]

    health_lines: List[str] = []
    health_file = local_raw_dir / "health.log"
    if health_file.exists():
        health_lines = [
            ln.strip()
            for ln in health_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]

    monitored_root = "/data/media/0"
    baseline_total = 100
    watchers_count = 0
    window_ms = 5000
    min_files = 6
    min_score = 15.0

    for ln in health_lines:
        if "STARTUP" in ln:
            for token in ln.split():
                if token.startswith("root="):
                    monitored_root = token.split("=", 1)[1]
                elif token.startswith("baseline_files="):
                    baseline_total = int(token.split("=", 1)[1])
                elif token.startswith("watchers="):
                    watchers_count = int(token.split("=", 1)[1])
                elif token.startswith("window_ms="):
                    window_ms = int(token.split("=", 1)[1])
                elif token.startswith("min_files="):
                    min_files = int(token.split("=", 1)[1])
                elif token.startswith("min_score="):
                    min_score = float(token.split("=", 1)[1])

    if baseline_json_path and baseline_json_path.exists() and baseline_total == 100:
        try:
            bdata = json.loads(baseline_json_path.read_text(encoding="utf-8"))
            baseline_total = int(bdata.get("file_count", baseline_total))
        except Exception:
            pass

    truth_runs: Dict[str, List[Dict[str, Any]]] = {}
    truth_dir = local_raw_dir / "truth"
    if truth_dir.exists():
        for tf in sorted(truth_dir.glob("*.jsonl")):
            records = load_jsonl(tf)
            if records:
                run_id = records[0].get("run_id", tf.stem)
                truth_runs[run_id] = records

    events.sort(key=lambda x: x.get("ts_ns", 0))
    alerts.sort(key=lambda x: x.get("ts_ns", 0))
    kills.sort(key=lambda x: x.get("ts_ns", 0))

    trials: List[Dict[str, Any]] = []
    if truth_runs:
        for idx, (run_id, trecords) in enumerate(truth_runs.items(), start=1):
            workload = trecords[0].get("workload", run_id)
            label = trecords[0].get("label", "attack" if workload.startswith("A") else "benign")
            t_start = trecords[0].get("ts_ns", 0)
            op_records = [r for r in trecords if r.get("op") not in ("start", "end", "skip")]
            first_op_ts = op_records[0]["ts_ns"] if op_records else t_start
            has_end_record = any(r.get("op") == "end" for r in trecords)
            t_end = trecords[-1].get("ts_ns", t_start) + 500_000_000

            seg_events = [e for e in events if (t_start - 100_000_000) <= e["ts_ns"] <= t_end]
            seg_alerts = [a for a in alerts if (t_start - 100_000_000) <= a["ts_ns"] <= t_end]
            seg_kills = [k for k in kills if (t_start - 100_000_000) <= k["ts_ns"] <= t_end]

            trials.append(
                compute_trial_metrics(
                    trial_num=idx,
                    scenario=workload,
                    label=label,
                    seg_events=seg_events,
                    seg_alerts=seg_alerts,
                    seg_kills=seg_kills,
                    truth_first_op_ns=first_op_ts,
                    truth_has_end=has_end_record,
                    baseline_total=baseline_total,
                )
            )
    else:
        segments: List[List[Dict[str, Any]]] = []
        cur_seg: List[Dict[str, Any]] = []
        for ev in events:
            if not cur_seg:
                cur_seg.append(ev)
            else:
                gap_ms = (ev["ts_ns"] - cur_seg[-1]["ts_ns"]) / 1e6
                prev_any_base = any(x.get("baseline") for x in cur_seg)
                if gap_ms > 650 or (not prev_any_base and ev.get("baseline") and gap_ms > 200):
                    segments.append(cur_seg)
                    cur_seg = [ev]
                else:
                    cur_seg.append(ev)
        if cur_seg:
            segments.append(cur_seg)

        for idx, seg_events in enumerate(segments, start=1):
            t0 = seg_events[0]["ts_ns"]
            t1 = seg_events[-1]["ts_ns"] + 500_000_000
            seg_alerts = [a for a in alerts if t0 <= a["ts_ns"] <= t1]
            seg_kills = [k for k in kills if t0 <= k["ts_ns"] <= t1]
            base_touched = {e["path"] for e in seg_events if e.get("baseline")}

            if seg_alerts:
                cands = seg_alerts[0].get("candidates", [])
                scenario = cands[0].get("name", f"Attack_{idx}") if cands else f"Attack_{idx}"
                label = "attack"
            elif len(base_touched) >= min_files:
                scenario = f"Attack_Burst_{idx}"
                label = "attack"
            else:
                scenario = "B1_Backup" if len(base_touched) == 0 else "B2_DocEdit"
                label = "benign"

            trials.append(
                compute_trial_metrics(
                    trial_num=idx,
                    scenario=scenario,
                    label=label,
                    seg_events=seg_events,
                    seg_alerts=seg_alerts,
                    seg_kills=seg_kills,
                    truth_first_op_ns=None,
                    truth_has_end=None,
                    baseline_total=baseline_total,
                )
            )

    tp = sum(1 for t in trials if t["label"] == "attack" and t["alerted"])
    fn = sum(1 for t in trials if t["label"] == "attack" and not t["alerted"])
    fp = sum(1 for t in trials if t["label"] == "benign" and t["alerted"])
    tn = sum(1 for t in trials if t["label"] == "benign" and not t["alerted"])

    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 1.0

    alerted_attacks = [t for t in trials if t["label"] == "attack" and t["alerted"]]
    mean_delay_ms = (
        sum(t["delay_ms"] for t in alerted_attacks) / len(alerted_attacks)
        if alerted_attacks
        else 0.0
    )
    mean_telemetry_delay_ms = (
        sum(t["telemetry_delay_ms"] for t in alerted_attacks) / len(alerted_attacks)
        if alerted_attacks
        else 0.0
    )
    mean_kill_latency_ms = (
        sum(t["kill_latency_ms"] for t in alerted_attacks) / len(alerted_attacks)
        if alerted_attacks
        else 0.0
    )
    mean_mitigation_ms = (
        sum(t["total_mitigation_ms"] for t in alerted_attacks) / len(alerted_attacks)
        if alerted_attacks
        else 0.0
    )
    mean_files_changed = (
        sum(t["files_changed"] for t in alerted_attacks) / len(alerted_attacks)
        if alerted_attacks
        else 0.0
    )
    benign_killed_total = sum(1 for t in trials if t["benign_killed"])

    event_type_counts = dict(collections.Counter(e.get("type", "UNKNOWN") for e in events))
    baseline_event_count = sum(1 for e in events if e.get("baseline"))
    non_baseline_event_count = len(events) - baseline_event_count

    peak_eps = 0
    for i, ev in enumerate(events):
        t_s = ev["ts_ns"]
        cnt = sum(1 for e2 in events[i:] if e2["ts_ns"] - t_s <= 1_000_000_000)
        if cnt > peak_eps:
            peak_eps = cnt

    # Build chronological timeline merging events, alerts, and kills
    timeline: List[Dict[str, Any]] = []
    t0_ns = events[0]["ts_ns"] if events else 0

    for ev in events:
        timeline.append({
            "ts_ns": ev["ts_ns"],
            "time": ns_to_time_str(ev["ts_ns"]),
            "rel_ms": round((ev["ts_ns"] - t0_ns) / 1e6, 2) if t0_ns else 0.0,
            "kind": "EVENT",
            "type": ev.get("type", "UNKNOWN"),
            "baseline": bool(ev.get("baseline", False)),
            "path": ev.get("path", ""),
            "info": "baseline_file" if ev.get("baseline") else "new_file",
        })

    for al in alerts:
        cands = ", ".join(
            f"{c.get('name')}(pid={c.get('pid')},wchar={c.get('total_wchar')})"
            for c in al.get("candidates", [])
        )
        timeline.append({
            "ts_ns": al["ts_ns"],
            "time": ns_to_time_str(al["ts_ns"]),
            "rel_ms": round((al["ts_ns"] - t0_ns) / 1e6, 2) if t0_ns else 0.0,
            "kind": "ALERT",
            "type": "ALERT_FIRED",
            "baseline": True,
            "path": f"score={al.get('score')} files_touched={al.get('files_touched')} window={al.get('window_ms')}ms",
            "info": f"reasons={al.get('reasons')} candidates=[{cands}]",
        })

    for kl in kills:
        timeline.append({
            "ts_ns": kl["ts_ns"],
            "time": ns_to_time_str(kl["ts_ns"]),
            "rel_ms": round((kl["ts_ns"] - t0_ns) / 1e6, 2) if t0_ns else 0.0,
            "kind": "KILL",
            "type": f"SIGKILL_{kl.get('result', 'ok').upper()}",
            "baseline": True,
            "path": f"killed_name={kl.get('name')} pid={kl.get('pid')}",
            "info": f"result={kl.get('result')}",
        })

    order_map = {"EVENT": 0, "ALERT": 1, "KILL": 2}
    timeline.sort(key=lambda x: (x["ts_ns"], order_map.get(x["kind"], 9)))

    return {
        "metadata": {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
            "monitored_root": monitored_root,
            "baseline_total_files": baseline_total,
            "watchers_registered": watchers_count,
            "window_ms": window_ms,
            "min_files_threshold": min_files,
            "min_score_threshold": min_score,
        },
        "performance_metrics": {
            "total_events_logged": len(events),
            "baseline_events": baseline_event_count,
            "non_baseline_events": non_baseline_event_count,
            "event_type_breakdown": event_type_counts,
            "peak_event_rate_per_sec": peak_eps,
            "total_alerts": len(alerts),
            "total_kills": len(kills),
            "TP": tp,
            "FP": fp,
            "TN": tn,
            "FN": fn,
            "precision_pct": round(precision * 100.0, 2),
            "recall_pct": round(recall * 100.0, 2),
            "f1_score_pct": round(f1 * 100.0, 2),
            "mean_detection_delay_ms": round(mean_delay_ms, 3),
            "mean_telemetry_delay_ms": round(mean_telemetry_delay_ms, 3),
            "mean_kill_latency_ms": round(mean_kill_latency_ms, 3),
            "mean_total_mitigation_ms": round(mean_mitigation_ms, 3),
            "mean_files_changed_before_kill": round(mean_files_changed, 2),
            "benign_workloads_killed": benign_killed_total,
        },
        "trials": trials,
        "health_log": health_lines,
        "chronological_logs": timeline,
    }


# ---------------------------------------------------------------------------
# Save Formatted .txt, .json, and .csv + Print to Console
# ---------------------------------------------------------------------------
def save_and_print_reports(report: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "formatted_logs.txt"
    json_path = out_dir / "formatted_logs.json"
    csv_path = out_dir / "metrics_summary.csv"

    # 1. Save structured JSON file
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # 2. Save CSV file (Section 7 spec)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trial", "scenario", "label", "alerted", "delay_ms",
            "kill_latency_ms", "files_changed", "killed_pid", "killed_name", "benign_killed"
        ])
        for t in report["trials"]:
            writer.writerow([
                t["trial"], t["scenario"], t["label"], t["alerted"], t["delay_ms"],
                t["kill_latency_ms"], t["files_changed"], t["killed_pid"], t["killed_name"], t["benign_killed"]
            ])

    # 3. Build clean, readable ASCII text file
    meta = report["metadata"]
    perf = report["performance_metrics"]
    lines: List[str] = []

    lines.append("=" * 108)
    lines.append("  PS-001 ANDROID RANSOMWARE EARLY-WARNING MONITOR — FORMATTED LOGS & PERFORMANCE METRICS")
    lines.append("=" * 108)
    lines.append(f"  Generated At        : {meta['generated_at']}")
    lines.append(f"  Monitored Root      : {meta['monitored_root']}  (Watchers: {meta['watchers_registered']}, Baseline Files: {meta['baseline_total_files']})")
    lines.append(f"  Detector Config     : window={meta['window_ms']}ms | min_files={meta['min_files_threshold']} | min_score={meta['min_score_threshold']}")
    lines.append("-" * 108)
    lines.append("  1. DERIVED PERFORMANCE METRICS")
    lines.append("-" * 108)
    lines.append(f"  * Precision / Recall / F1     : {perf['precision_pct']:.1f}%  /  {perf['recall_pct']:.1f}%  /  {perf['f1_score_pct']:.1f}%   (TP={perf['TP']}, FP={perf['FP']}, TN={perf['TN']}, FN={perf['FN']})")
    lines.append(f"  * Mean Detection Delay        : {perf['mean_detection_delay_ms']:.3f} ms   (First attack file op -> Alert fired)")
    lines.append(f"  * Mean Telemetry Window Delay : {perf['mean_telemetry_delay_ms']:.3f} ms   (First baseline event in window -> Alert fired)")
    lines.append(f"  * Mean Kill Latency           : {perf['mean_kill_latency_ms']:.3f} ms   (Alert fired -> /proc attribution -> SIGKILL)")
    lines.append(f"  * Mean Total Mitigation Time  : {perf['mean_total_mitigation_ms']:.3f} ms   (First attack op -> Attacker process dead)")
    lines.append(f"  * Mean Files Changed (<Kill)  : {perf['mean_files_changed_before_kill']:.1f} / {meta['baseline_total_files']} files")
    lines.append(f"  * Benign Workloads Killed     : {perf['benign_workloads_killed']}   (Requirement: 0)")
    lines.append(f"  * Telemetry Volume & Rate     : {perf['total_events_logged']} events (Baseline={perf['baseline_events']}, New={perf['non_baseline_events']}) | Peak={perf['peak_event_rate_per_sec']} events/sec")
    lines.append(f"  * Event Type Breakdown        : {json.dumps(perf['event_type_breakdown'])}")
    lines.append("-" * 108)
    lines.append("  2. PER-WORKLOAD / TRIAL EVALUATION SUMMARY")
    lines.append("-" * 108)
    lines.append(
        f"  {'#':<3} {'Workload':<12} {'Label':<8} {'Res':<4} {'Alert':<7} "
        f"{'Delay(ms)':<11} {'KillLat(ms)':<12} {'Mitig(ms)':<11} {'Changed':<9} {'Saved%':<8} {'Killed Process'}"
    )
    lines.append("  " + "-" * 104)

    for t in report["trials"]:
        kproc = f"PID {t['killed_pid']} ({t['killed_name']}) [{t['kill_result']}]" if t["killed_pid"] > 0 else "None"
        changed_str = f"{t['files_changed']}/{meta['baseline_total_files']}"
        lines.append(
            f"  {t['trial']:<3} {t['scenario']:<12} {t['label']:<8} {t['outcome']:<4} {str(t['alerted']):<7} "
            f"{t['delay_ms']:<11.3f} {t['kill_latency_ms']:<12.3f} {t['total_mitigation_ms']:<11.3f} "
            f"{changed_str:<9} {t['survival_pct']:<8.1f} {kproc}"
        )
        if t["reasons"]:
            lines.append(f"      -> Alert Score: {t['score']} | Reasons: {', '.join(t['reasons'])}")
        if t["touched_files_list"]:
            lines.append(f"      -> Baseline Files Touched ({len(t['touched_files_list'])}): {', '.join(t['touched_files_list'])}")

    lines.append("-" * 108)
    lines.append(f"  3. CHRONOLOGICAL TELEMETRY LOG STREAM ({len(report['chronological_logs'])} entries)")
    lines.append("-" * 108)
    lines.append(f"  {'#':<4} {'Timestamp':<16} {'Rel(ms)':<10} {'Kind':<7} {'Event Type':<14} {'Base':<6} {'Path / Details'}")
    lines.append("  " + "-" * 104)

    for idx, entry in enumerate(report["chronological_logs"], start=1):
        base_str = "YES" if entry["baseline"] else "NO"
        rel_str = f"+{entry['rel_ms']:.2f}"
        if entry["kind"] in ("ALERT", "KILL"):
            lines.append("  " + "*" * 104)
            lines.append(
                f"  {idx:<4} {entry['time']:<16} {rel_str:<10} {entry['kind']:<7} {entry['type']:<14} {base_str:<6} {entry['path']}"
            )
            lines.append(f"       >> {entry['info']}")
            lines.append("  " + "*" * 104)
        else:
            lines.append(
                f"  {idx:<4} {entry['time']:<16} {rel_str:<10} {entry['kind']:<7} {entry['type']:<14} {base_str:<6} {entry['path']}"
            )

    lines.append("=" * 108)

    formatted_text = "\n".join(lines)
    txt_path.write_text(formatted_text, encoding="utf-8")

    # Print directly to terminal
    print("\n" + formatted_text + "\n")
    print(f"[+] Saved formatted text log   : {txt_path}")
    print(f"[+] Saved structured JSON log  : {json_path}")
    print(f"[+] Saved evaluation CSV       : {csv_path}")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Format RadarMonitor logs into .txt/.json and compute performance metrics.")
    ap.add_argument("--device-logs", default="/data/local/tmp/radar_logs", help="Device monitor log directory")
    ap.add_argument("--device-reg", default="/data/local/tmp/radar_registry", help="Device PID registry directory")
    ap.add_argument("--device-truth", default="/data/local/tmp/radar_truth", help="Device ground-truth log directory")
    ap.add_argument("--out-dir", default="radar_reports", help="Laptop output folder")
    ap.add_argument("--baseline", default="baseline.json", help="Path to local baseline.json")
    ap.add_argument("--demo", action="store_true", help="Run a fresh non-destructive B1+A3 capture on the emulator first")
    ap.add_argument("--no-pull", action="store_true", help="Analyze already-pulled logs in radar_reports/raw_logs")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    raw_dir = out_dir / "raw_logs"

    if not args.no_pull:
        if args.demo or not device_logs_exist(args.device_logs):
            run_live_demo_trial_on_device(args.device_logs, args.device_reg, args.device_truth, raw_dir)
        print(f"[*] Pulling telemetry logs from {args.device_logs} -> {raw_dir} ...")
        pull_logs_from_device(args.device_logs, args.device_truth, raw_dir)

    baseline_path = Path(args.baseline)
    if not baseline_path.exists() and Path("phone_env/baseline.json").exists():
        baseline_path = Path("phone_env/baseline.json")

    report = analyze_logs(raw_dir, baseline_path)
    save_and_print_reports(report, out_dir)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
measure_overhead.py - Resource overhead of the MonitorKt daemon on the Android lab device.

Answers: "how much CPU / memory / storage does the detector itself cost, and does it
slow down normal workloads?"  Uses only the standard library plus adb (root).

Subcommands
-----------
  idle      Sample the running daemon with no workload running (steady-state cost).
  load      Sample the running daemon while workloads run (cost under file activity).
  slowdown  Run benign workloads with the monitor OFF vs ON and compare run time.
  all       idle + load + slowdown, then one combined summary.

What is measured (all read from /proc on the device, as root):
  * CPU of the monitor process: % of one core and % of the whole device, from
    utime+stime ticks divided by total CPU ticks over the same interval (so adb
    latency does not skew it).
  * Memory: VmRSS (resident), VmHWM (peak resident) and Pss when smaps_rollup exists.
  * Threads, open file descriptors, and inotify watches held by the monitor.
  * System-wide CPU busy % and MemAvailable, for context.
  * Log storage written by the monitor (bytes per event).
  * Workload slowdown: device-side wall time of benign workloads, monitor OFF vs ON.

Outputs go to ./overhead_reports (override with --out-dir):
  overhead_samples.csv   one row per sample
  overhead_summary.json  machine-readable summary
  overhead_summary.md    paste-ready tables for the README / PDF

Run from anywhere:  python measure_overhead.py all --trials 3
Prerequisites: emulator running, `adb root` working, dataset pushed
(make_phone_env.py --push), workloads pushed (push_workloads.ps1), and for `idle`
and `load` the daemon already running (monitor\\start_daemon.ps1).
Numbers from an emulator depend on the host PC; state that when you quote them.
"""
import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LOG_DIR = "/data/local/tmp/radar_logs"
JAR = "/data/local/tmp/radar.jar"
CLK_TCK = 100  # jiffies per second on Android/Linux; only used for the ms figure

# Bracketed pattern so the shell running this command never matches itself.
FIND_PID_CMD = (
    "for d in /proc/[0-9]*; do "
    "c=$(tr '\\0' ' ' < $d/cmdline 2>/dev/null); "
    "case \"$c\" in app_process*MonitorK[t]*) echo ${d#/proc/};; esac; "
    "done"
)


def adb_shell(cmd, timeout=30):
    r = subprocess.run(["adb", "shell", cmd], capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    return r.stdout.replace("\r", "")


def find_monitor_pid():
    out = adb_shell(FIND_PID_CMD).split()
    return int(out[0]) if out else None


def sample_cmd(pid):
    return (
        f"echo '##stat'; cat /proc/{pid}/stat 2>/dev/null; "
        f"echo '##status'; grep -E '^(VmRSS|VmHWM|Threads):' /proc/{pid}/status 2>/dev/null; "
        f"echo '##pss'; grep -E '^Pss:' /proc/{pid}/smaps_rollup 2>/dev/null; "
        f"echo '##fds'; ls /proc/{pid}/fd 2>/dev/null | wc -l; "
        f"echo '##inotify'; cat /proc/{pid}/fdinfo/* 2>/dev/null | grep -c '^inotify'; "
        "echo '##cpu'; head -1 /proc/stat; "
        "echo '##ncpu'; grep -c '^cpu[0-9]' /proc/stat; "
        "echo '##mem'; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo"
    )


def _kb(line):
    return int(line.split()[1])


def parse_sample(text):
    """Turn the raw '##section' text from sample_cmd into a dict (None if the pid vanished)."""
    sec, cur = {}, None
    for line in text.splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur is not None and line.strip():
            sec[cur].append(line.strip())
    if not sec.get("stat"):
        return None
    stat = sec["stat"][0]
    rest = stat[stat.rfind(")") + 1:].split()  # fields from #3 onward; comm may contain spaces
    s = {
        "proc_ticks": int(rest[11]) + int(rest[12]),  # utime + stime (fields 14, 15)
        "threads_stat": int(rest[17]),                # num_threads (field 20)
    }
    for line in sec.get("status", []):
        if line.startswith("VmRSS"):
            s["rss_kb"] = _kb(line)
        elif line.startswith("VmHWM"):
            s["hwm_kb"] = _kb(line)
        elif line.startswith("Threads"):
            s["threads"] = int(line.split()[1])
    s["pss_kb"] = _kb(sec["pss"][0]) if sec.get("pss") else None
    s["fds"] = int(sec["fds"][0]) if sec.get("fds") else None
    s["inotify_watches"] = int(sec["inotify"][0]) if sec.get("inotify") else None
    cpu = [int(x) for x in sec["cpu"][0].split()[1:]]
    s["total_ticks"] = sum(cpu[:8])               # user nice system idle iowait irq softirq steal
    s["idle_ticks"] = cpu[3] + (cpu[4] if len(cpu) > 4 else 0)
    s["ncpu"] = int(sec["ncpu"][0]) if sec.get("ncpu") else 1
    for line in sec.get("mem", []):
        if line.startswith("MemAvailable"):
            s["mem_avail_kb"] = _kb(line)
        elif line.startswith("MemTotal"):
            s["mem_total_kb"] = _kb(line)
    s.setdefault("threads", s["threads_stat"])
    return s


class Sampler(threading.Thread):
    """Samples the monitor every `interval` seconds until stop() is called."""

    def __init__(self, interval=1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.phase = "idle"
        self.rows = []
        self.missing = 0
        self._halt = threading.Event()
        self.pid = None

    def stop(self):
        self._halt.set()
        self.join(timeout=10)

    def run(self):
        prev = None
        t0 = time.time()
        while not self._halt.is_set():
            started = time.time()
            if self.pid is None:
                self.pid = find_monitor_pid()
            cur = parse_sample(adb_shell(sample_cmd(self.pid))) if self.pid else None
            if cur is None:
                self.pid = None
                self.missing += 1
                prev = None
            else:
                if prev is not None and cur["total_ticks"] > prev["total_ticks"]:
                    dt = cur["total_ticks"] - prev["total_ticks"]
                    dp = cur["proc_ticks"] - prev["proc_ticks"]
                    busy = dt - (cur["idle_ticks"] - prev["idle_ticks"])
                    self.rows.append({
                        "t_s": round(time.time() - t0, 2),
                        "phase": self.phase,
                        "pid": self.pid,
                        "cpu_pct_of_core": round(100.0 * dp / dt * cur["ncpu"], 2),
                        "cpu_pct_of_device": round(100.0 * dp / dt, 2),
                        "sys_busy_pct": round(100.0 * busy / dt, 2),
                        "rss_kb": cur.get("rss_kb"),
                        "hwm_kb": cur.get("hwm_kb"),
                        "pss_kb": cur.get("pss_kb"),
                        "threads": cur.get("threads"),
                        "fds": cur.get("fds"),
                        "inotify_watches": cur.get("inotify_watches"),
                        "mem_avail_kb": cur.get("mem_avail_kb"),
                    })
                prev = cur
            self._halt.wait(max(0.0, self.interval - (time.time() - started)))


def pct(values, p):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(round((p / 100.0) * (len(v) - 1))))]


def summarize_phase(rows):
    def col(name):
        return [r[name] for r in rows if r.get(name) is not None]
    cpu, rss = col("cpu_pct_of_core"), col("rss_kb")
    if not rows:
        return {"samples": 0}
    return {
        "samples": len(rows),
        "cpu_core_mean_pct": round(statistics.mean(cpu), 2),
        "cpu_core_p95_pct": round(pct(cpu, 95), 2),
        "cpu_core_max_pct": round(max(cpu), 2),
        "cpu_device_mean_pct": round(statistics.mean(col("cpu_pct_of_device")), 2),
        "rss_mean_mb": round(statistics.mean(rss) / 1024, 1) if rss else None,
        "rss_max_mb": round(max(rss) / 1024, 1) if rss else None,
        "hwm_max_mb": round(max(col("hwm_kb")) / 1024, 1) if col("hwm_kb") else None,
        "pss_max_mb": round(max(col("pss_kb")) / 1024, 1) if col("pss_kb") else None,
        "threads_max": max(col("threads")) if col("threads") else None,
        "fds_max": max(col("fds")) if col("fds") else None,
        "inotify_watches": max(col("inotify_watches")) if col("inotify_watches") else None,
        "system_busy_mean_pct": round(statistics.mean(col("sys_busy_pct")), 2),
    }


def log_storage():
    out = adb_shell(
        f"for f in events.jsonl alerts.jsonl kill.jsonl health.log; do "
        f"printf '%s ' $f; stat -c %s {LOG_DIR}/$f 2>/dev/null || echo 0; done; "
        f"printf 'event_lines '; wc -l < {LOG_DIR}/events.jsonl 2>/dev/null || echo 0")
    sizes, events = {}, 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "event_lines":
            events = int(parts[1])
        elif len(parts) == 2:
            sizes[parts[0]] = int(parts[1])
    total = sum(sizes.values())
    return {
        "bytes_by_file": sizes,
        "total_bytes": total,
        "event_lines": events,
        "bytes_per_event": round(sizes.get("events.jsonl", 0) / events, 1) if events else None,
    }


# ---------------------------------------------------------------- phases

def run_idle(sampler, seconds):
    print(f"[idle] sampling the idle daemon for {seconds}s ...")
    sampler.phase = "idle"
    time.sleep(seconds)


def run_load(sampler, workloads, trials):
    import launch_workload as lw
    for t in range(1, trials + 1):
        for key in workloads:
            print(f"[load] trial {t}/{trials}: {key}")
            sampler.phase = "reset"
            lw.reset_dataset()
            time.sleep(1.5)
            sampler.phase = f"load:{key}"
            lw.run_one(key, reset=False)
            time.sleep(2.0)  # let the monitor drain its queue and write the kill record
    sampler.phase = "idle"


def timed_workload_ms(key):
    """Run one workload on the device and return its wall time in ms, measured on the device."""
    import launch_workload as lw
    name, script, extra_env = lw.WORKLOADS[key]
    run_id = f"ovh_{key.lower()}_{int(time.time())}"
    cmd = (f"s=$(date +%s%N); RUN_ID={run_id} {extra_env} "
           f"sh /data/local/tmp/workloads/launcher.sh {name} {script} >/dev/null 2>&1; "
           f"e=$(date +%s%N); echo $(( (e - s) / 1000000 ))")
    out = adb_shell(cmd, timeout=300).strip().splitlines()
    return float(out[-1]) if out else float("nan")


def stop_monitor():
    adb_shell("pkill -f 'MonitorK[t]' 2>/dev/null; true")
    time.sleep(0.5)


def start_monitor(root, window_ms, min_files, min_score):
    adb_shell(
        "nohup sh -c 'CLASSPATH=%s app_process /system/bin MonitorKt --root %s --window-ms %d "
        "--min-files %d --min-score %s --kill true >/dev/null 2>&1' >/dev/null 2>&1 &"
        % (JAR, root, window_ms, min_files, min_score))
    time.sleep(2.5)  # the monitor scans the baseline at startup; wait for it
    if find_monitor_pid() is None:
        raise SystemExit("Monitor failed to start. Run monitor\\start_daemon.ps1 once to push radar.jar.")


def run_slowdown(workloads, trials, args):
    import launch_workload as lw
    result = {k: {"off_ms": [], "on_ms": []} for k in workloads}
    for t in range(1, trials + 1):
        for key in workloads:
            # Alternate the order each trial (OFF,ON then ON,OFF) so drift does not favour one mode.
            order = ("off", "on") if t % 2 else ("on", "off")
            for mode in order:
                stop_monitor()
                lw.reset_dataset()
                time.sleep(1.0)
                if mode == "on":
                    start_monitor(args.root, args.window_ms, args.min_files, args.min_score)
                ms = timed_workload_ms(key)
                print(f"[slowdown] trial {t} {key} monitor {mode.upper():3}: {ms:.0f} ms")
                result[key][mode + "_ms"].append(ms)
    stop_monitor()
    start_monitor(args.root, args.window_ms, args.min_files, args.min_score)  # leave the daemon running
    out = {}
    for key, d in result.items():
        off, on = statistics.mean(d["off_ms"]), statistics.mean(d["on_ms"])
        out[key] = {"off_mean_ms": round(off, 1), "on_mean_ms": round(on, 1),
                    "overhead_pct": round(100.0 * (on - off) / off, 2) if off else None,
                    "off_ms": d["off_ms"], "on_ms": d["on_ms"]}
    return out


# ---------------------------------------------------------------- output

def write_outputs(out_dir, rows, summary):
    out_dir.mkdir(parents=True, exist_ok=True)
    if rows:
        with open(out_dir / "overhead_samples.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    (out_dir / "overhead_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    md = ["# Monitor overhead", "",
          f"Device: {summary['device']}  |  CPUs: {summary['ncpu']}  |  sample interval: {summary['interval_s']} s", "",
          "## CPU and memory of the MonitorKt process", "",
          "| Phase | Samples | CPU mean (% of 1 core) | CPU p95 | CPU max | RSS mean (MB) | RSS max (MB) | Peak RSS (MB) | Threads | FDs | inotify watches |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for phase, s in summary["phases"].items():
        if s.get("samples", 0) == 0:
            continue
        md.append(f"| {phase} | {s['samples']} | {s['cpu_core_mean_pct']} | {s['cpu_core_p95_pct']} | "
                  f"{s['cpu_core_max_pct']} | {s['rss_mean_mb']} | {s['rss_max_mb']} | {s['hwm_max_mb']} | "
                  f"{s['threads_max']} | {s['fds_max']} | {s['inotify_watches']} |")
    st = summary.get("log_storage")
    if st:
        md += ["", "## Log storage written by the monitor", "",
               f"- Total: {st['total_bytes']} bytes across {st['event_lines']} event lines"
               + (f" ({st['bytes_per_event']} bytes/event in events.jsonl)" if st['bytes_per_event'] else ""),
               f"- By file: {json.dumps(st['bytes_by_file'])}"]
    sl = summary.get("slowdown")
    if sl:
        md += ["", "## Benign workload slowdown (device-side wall time)", "",
               "| Workload | Monitor OFF (ms) | Monitor ON (ms) | Overhead |", "|---|---:|---:|---:|"]
        for k, v in sl.items():
            md.append(f"| {k} | {v['off_mean_ms']} | {v['on_mean_ms']} | {v['overhead_pct']}% |")
    md += ["", "_Emulator figures depend on the host PC; quote them together with the host CPU and the emulator "
               "image. CPU % is utime+stime of the monitor over total device CPU ticks in the same interval._"]
    (out_dir / "overhead_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\n[+] Wrote {out_dir}\\overhead_samples.csv, overhead_summary.json, overhead_summary.md")


def main():
    ap = argparse.ArgumentParser(description="Measure MonitorKt resource overhead on the lab device")
    ap.add_argument("mode", choices=["idle", "load", "slowdown", "all"])
    ap.add_argument("--duration", type=int, default=60, help="idle sampling seconds (default 60)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples (default 1)")
    ap.add_argument("--trials", type=int, default=3, help="repetitions for load/slowdown (default 3)")
    ap.add_argument("--workloads", nargs="+", default=None,
                    help="load: any of A1 A2 A3 A3_slow B1 B2 E1-E4 (default B1 B2 A1 A2 A3); "
                         "slowdown: benign only (default B1 B2)")
    ap.add_argument("--out-dir", default="overhead_reports")
    ap.add_argument("--root", default="/data/media/0")
    ap.add_argument("--window-ms", type=int, default=5000)
    ap.add_argument("--min-files", type=int, default=6)
    ap.add_argument("--min-score", type=float, default=15.0)
    args = ap.parse_args()

    subprocess.run(["adb", "root"], capture_output=True)
    subprocess.run(["adb", "wait-for-device"], capture_output=True)
    props = adb_shell("getprop ro.product.model; getprop ro.build.version.sdk").split("\n")
    summary = {"device": (props[0].strip() + " (API " + props[1].strip() + ")") if len(props) > 1 and props[1].strip() else "unknown",
               "interval_s": args.interval, "ncpu": 0, "phases": {}}
    sampler = None
    rows = []

    if args.mode in ("idle", "load", "all"):
        if find_monitor_pid() is None:
            raise SystemExit("MonitorKt is not running. Start it with monitor\\start_daemon.ps1 first.")
        summary["ncpu"] = parse_sample(adb_shell(sample_cmd(find_monitor_pid())))["ncpu"]
        sampler = Sampler(args.interval)
        sampler.start()
        if args.mode in ("idle", "all"):
            run_idle(sampler, args.duration)
        if args.mode in ("load", "all"):
            run_load(sampler, args.workloads or ["B1", "B2", "A1", "A2", "A3"], args.trials)
        sampler.stop()
        rows = sampler.rows
        by_phase = {}
        for r in rows:
            by_phase.setdefault(r["phase"], []).append(r)
        for phase in sorted(by_phase):
            summary["phases"][phase] = summarize_phase(by_phase[phase])
        if sampler.missing:
            summary["note"] = (f"{sampler.missing} sample(s) found no monitor process "
                               "(e.g. restarted or crashed) and were skipped")
        summary["log_storage"] = log_storage()

    if args.mode in ("slowdown", "all"):
        wl = args.workloads if (args.mode == "slowdown" and args.workloads) else ["B1", "B2"]
        summary["slowdown"] = run_slowdown(wl, args.trials, args)

    write_outputs(Path(args.out_dir), rows, summary)


if __name__ == "__main__":
    main()

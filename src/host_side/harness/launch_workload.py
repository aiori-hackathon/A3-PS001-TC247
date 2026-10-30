#!/usr/bin/env python3
"""
launch_workload.py - Window 2 Attack & Benign Workload Launcher.
Safely resets /sdcard using the daemon's .resetting flag (so the always-on
MonitorKt daemon never needs to restart) and launches any workload on the emulator.
"""
import argparse, subprocess, sys, time

WORKLOADS = {
    "A1":      ("A1",          "/data/local/tmp/workloads/A1_overwrite.sh",        ""),
    "A2":      ("A2",          "/data/local/tmp/workloads/A2_encrypt_delete.sh",   ""),
    "A3":      ("A3",          "/data/local/tmp/workloads/A3_overwrite_rename.sh", "SPEED=fast"),
    "A3_slow": ("A3_slow",     "/data/local/tmp/workloads/A3_overwrite_rename.sh", "SPEED=slow"),
    "B1":      ("B1",          "/data/local/tmp/workloads/B1_backup_copy.sh",      ""),
    "B2":      ("B2",          "/data/local/tmp/workloads/B2_doc_edit.sh",         ""),
    "E1":      ("E1_slow_drip","/data/local/tmp/workloads/E1_slow_drip.sh",        ""),
    "E2":      ("E2_temp",     "/data/local/tmp/workloads/E2_temp_stage_delete.sh",""),
    "E3":      ("E3_blitz",    "/data/local/tmp/workloads/E3_microfile_blitz.sh",   ""),
    "E4":      ("E4_detached", "/data/local/tmp/workloads/E4_subshell_worker.sh",   ""),
}

def adb(*args):
    return subprocess.run(["adb", *args], capture_output=True, text=True)

def reset_dataset():
    print("[Reset] Setting .resetting flag on emulator & restoring 100 baseline files...")
    adb("shell", "mkdir -p /data/local/tmp/radar_registry && touch /data/local/tmp/radar_registry/.resetting")
    adb("shell", "find /sdcard -name '*.locked' -o -name '*.enc' -o -name '*.tmp' -o -name 'README_RESTORE.txt' 2>/dev/null | xargs rm -f")
    adb("shell", "rm -rf /sdcard/Backups/run_* /sdcard/Backups/trial_*")
    subprocess.run([sys.executable, "make_phone_env.py", "--push"], stdout=subprocess.DEVNULL)
    time.sleep(0.4)
    adb("shell", "rm -f /data/local/tmp/radar_registry/.resetting")
    print("[Reset] Dataset clean (100 files ready).")

def run_one(key: str, reset: bool = True):
    if key not in WORKLOADS:
        print(f"Unknown workload '{key}'. Choose from: {', '.join(WORKLOADS.keys())}")
        sys.exit(1)

    if reset:
        reset_dataset()

    name, script, extra_env = WORKLOADS[key]
    run_id = f"trial_{key.lower()}_{int(time.time())}"
    cmd = f"RUN_ID={run_id} {extra_env} sh /data/local/tmp/workloads/launcher.sh {name} {script}"
    print(f"[Launch] Running {key} (RUN_ID={run_id}) -> Watch Window 1 for live logs & Incident Report!")
    r = subprocess.run(["adb", "shell", cmd])
    print(f"[Done] Workload {key} exited (device return code: {r.returncode})\n")

def main():
    ap = argparse.ArgumentParser(description="Window 2 Launcher for PS-001 Workloads")
    ap.add_argument("workload", nargs="?", help="Workload ID: A1, A2, A3, A3_slow, B1, B2, E1, E2, E3, E4")
    ap.add_argument("--no-reset", action="store_true", help="Skip resetting /sdcard before launch")
    ap.add_argument("--all", action="store_true", help="Run all 6 core scenarios (B1, B2, A1, A2, A3, A3_slow)")
    ap.add_argument("--trials", type=int, default=1, help="Number of repetitions when using --all")
    args = ap.parse_args()

    if args.all:
        sequence = ["B1", "B2", "A1", "A2", "A3", "A3_slow"]
        for t in range(1, args.trials + 1):
            print(f"=== Batch Trial Round {t}/{args.trials} ===")
            for wk in sequence:
                run_one(wk, reset=True)
                time.sleep(2.0)
        return

    if not args.workload:
        ap.print_help()
        return

    run_one(args.workload, reset=not args.no_reset)

if __name__ == "__main__":
    main()
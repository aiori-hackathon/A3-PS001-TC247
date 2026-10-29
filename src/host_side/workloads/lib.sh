#!/system/bin/sh
# Shared helper library for PS-001 workloads (POSIX sh / mksh + toybox)

TARGET_ROOT="${TARGET_ROOT:-/sdcard}"
TARGET_ROOT="${TARGET_ROOT%/}"
TRUTH_DIR="/data/local/tmp/radar_truth"
mkdir -p "$TRUTH_DIR"

now_ns() {
    date +%s%N
}

if [ -z "$RUN_ID" ]; then
    RUN_ID="run_$(date +%s)"
fi
TRUTH_FILE="$TRUTH_DIR/${RUN_ID}.jsonl"

# Lab safety check: ensure we are on the Android emulator and inside the dummy dataset
verify_lab_env() {
    if [ ! -d "$TARGET_ROOT/DCIM" ] && [ ! -d "$TARGET_ROOT/Documents" ]; then
        echo "ERROR: Dummy dataset folders not found under $TARGET_ROOT" >&2
        exit 1
    fi
}

# JSON string escaping for paths
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# Atomic single-printf append to ground-truth log (Section 4 of WORKLOADS_CONTEXT.md)
log_truth() {
    _op="$1"
    _path="$2"
    _new_path="$3"
    _ts="$(now_ns)"
    _esc_run="$(json_escape "$RUN_ID")"
    _esc_wk="$(json_escape "$WORKLOAD_NAME")"
    _esc_lbl="$(json_escape "$WORKLOAD_LABEL")"

    if [ -n "$_new_path" ]; then
        _esc_p="$(json_escape "$_path")"
        _esc_np="$(json_escape "$_new_path")"
        printf '{"ts_ns":%s,"run_id":"%s","workload":"%s","label":"%s","op":"%s","path":"%s","new_path":"%s"}\n' \
            "$_ts" "$_esc_run" "$_esc_wk" "$_esc_lbl" "$_op" "$_esc_p" "$_esc_np" >> "$TRUTH_FILE"
    elif [ -n "$_path" ]; then
        _esc_p="$(json_escape "$_path")"
        printf '{"ts_ns":%s,"run_id":"%s","workload":"%s","label":"%s","op":"%s","path":"%s"}\n' \
            "$_ts" "$_esc_run" "$_esc_wk" "$_esc_lbl" "$_op" "$_esc_p" >> "$TRUTH_FILE"
    else
        printf '{"ts_ns":%s,"run_id":"%s","workload":"%s","label":"%s","op":"%s"}\n' \
            "$_ts" "$_esc_run" "$_esc_wk" "$_esc_lbl" "$_op" >> "$TRUTH_FILE"
    fi
}

# Deterministic file listing across media & document folders (excludes .locked/.enc/.tmp)
list_attack_targets() {
    _max="$1"
    find "$TARGET_ROOT/DCIM" \
         "$TARGET_ROOT/Pictures" \
         "$TARGET_ROOT/Documents" \
         "$TARGET_ROOT/Download" \
         "$TARGET_ROOT/Android/media" \
         "$TARGET_ROOT/Backups" \
         -type f \
         ! -name "*.locked" \
         ! -name "*.enc" \
         ! -name "*.tmp" \
         ! -name "README_RESTORE.txt" 2>/dev/null | sort | head -n "$_max"
}

file_size() {
    stat -c %s "$1" 2>/dev/null || wc -c < "$1"
}
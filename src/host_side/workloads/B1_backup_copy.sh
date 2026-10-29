#!/system/bin/sh
# B1: Benign photo backup - copies 30 JPGs from DCIM/Camera to Backups/<RUN_ID>/
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="B1"
WORKLOAD_LABEL="benign"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-30}"
DELAY="${DELAY:-0.1}"

verify_lab_env
BACKUP_DEST="$TARGET_ROOT/Backups/$RUN_ID"
mkdir -p "$BACKUP_DEST"
log_truth "start"

OLD_IFS="$IFS"
IFS='
'
for f in $(find "$TARGET_ROOT/DCIM/Camera" -type f -name "*.jpg" 2>/dev/null | sort | head -n "$MAX_FILES"); do
    IFS="$OLD_IFS"
    if [ ! -f "$f" ]; then
        log_truth "skip" "$f"
        continue
    fi
    base="$(basename "$f")"
    dst="$BACKUP_DEST/$base"
    cp "$f" "$dst"
    log_truth "copy" "$f" "$dst"
    sleep "$DELAY"
done
IFS="$OLD_IFS"

log_truth "end"
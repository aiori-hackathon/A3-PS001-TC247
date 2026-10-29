#!/system/bin/sh
# B2: Benign document editing - appends a line to 8 TXT/CSV files and performs atomic .tmp save
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="B2"
WORKLOAD_LABEL="benign"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-8}"
DELAY="${DELAY:-1.5}"

verify_lab_env
log_truth "start"

OLD_IFS="$IFS"
IFS='
'
for f in $(find "$TARGET_ROOT/Documents" \( -name "*.txt" -o -name "*.csv" \) -type f 2>/dev/null | sort | head -n "$MAX_FILES"); do
    IFS="$OLD_IFS"
    if [ ! -f "$f" ]; then
        log_truth "skip" "$f"
        continue
    fi
    printf 'edit %s\n' "$RUN_ID" >> "$f"
    log_truth "modify" "$f"

    tmp="${f}.tmp"
    cp "$f" "$tmp"
    log_truth "create" "$tmp"
    mv "$tmp" "$f"
    log_truth "rename" "$tmp" "$f"

    sleep "$DELAY"
done
IFS="$OLD_IFS"

log_truth "end"
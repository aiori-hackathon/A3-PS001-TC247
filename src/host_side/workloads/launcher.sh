#!/system/bin/sh
# usage: launcher.sh <NAME> <SCRIPT> [args...]
REG="/data/local/tmp/radar_registry"
name="$1"
script="$2"
if [ -z "$name" ] || [ -z "$script" ]; then
    echo "Usage: launcher.sh <NAME> <SCRIPT> [args...]" >&2
    exit 1
fi
shift 2

mkdir -p "$REG" /data/local/tmp/radar_truth
rm -f "$REG/$name.pid" "$REG/$name.start"

sh "$script" "$@" &
child_pid=$!
printf '%s\n' "$child_pid" > "$REG/$name.pid"
date +%s%N > "$REG/$name.start"

wait "$child_pid"
rc=$?
rm -f "$REG/$name.pid"
exit "$rc"
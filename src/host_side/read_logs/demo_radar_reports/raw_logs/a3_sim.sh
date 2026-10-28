#!/system/bin/sh
t0=$(date +%s%N)
printf '{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"start"}\n' "$t0" >> /data/local/tmp/radar_truth/run_a3.jsonl
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    f="/data/media/0/radar_ui_sandbox/Documents/note_$i.txt"
    head -c 512 /dev/urandom > "$f"
    ts1=$(date +%s%N)
    printf '{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"overwrite","path":"%s"}\n' "$ts1" "$f" >> /data/local/tmp/radar_truth/run_a3.jsonl
    mv "$f" "$f.locked"
    ts2=$(date +%s%N)
    printf '{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"rename","path":"%s","new_path":"%s.locked"}\n' "$ts2" "$f" "$f" >> /data/local/tmp/radar_truth/run_a3.jsonl
    sleep 0.1
done
t1=$(date +%s%N)
printf '{"ts_ns":%s,"run_id":"run_a3","workload":"A3","label":"attack","op":"end"}\n' "$t1" >> /data/local/tmp/radar_truth/run_a3.jsonl

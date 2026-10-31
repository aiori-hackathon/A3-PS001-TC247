#!/system/bin/sh
mkdir -p /data/media/0/radar_ui_sandbox/Backups/run_b1
t0=$(date +%s%N)
printf '{"ts_ns":%s,"run_id":"run_b1","workload":"B1","label":"benign","op":"start"}\n' "$t0" >> /data/local/tmp/radar_truth/run_b1.jsonl
for i in 1 2 3 4 5 6 7 8 9 10; do
    echo "backup_image_data_$i" > "/data/media/0/radar_ui_sandbox/Backups/run_b1/IMG_00$i.jpg"
    ts=$(date +%s%N)
    printf '{"ts_ns":%s,"run_id":"run_b1","workload":"B1","label":"benign","op":"copy","path":"/data/media/0/radar_ui_sandbox/Backups/run_b1/IMG_00%d.jpg"}\n' "$ts" "$i" >> /data/local/tmp/radar_truth/run_b1.jsonl
    sleep 0.05
done
t1=$(date +%s%N)
printf '{"ts_ns":%s,"run_id":"run_b1","workload":"B1","label":"benign","op":"end"}\n' "$t1" >> /data/local/tmp/radar_truth/run_b1.jsonl

#!/usr/bin/env python3
"""
make_phone_env.py - build a realistic, *benign* Android-style user-data tree
for the PS-001 ransomware-behaviour lab, plus a baseline manifest.

Usage:
  python make_phone_env.py                 # generate ./phone_env + manifest
  python make_phone_env.py --push          # also adb push to the emulator
  python make_phone_env.py --verify        # compare device files against manifest
  python make_phone_env.py --seed 7 --device-root /sdcard

Stdlib only (Pillow optional: gives real JPEG photos if installed).
"""
import argparse, csv, hashlib, io, json, math, os, random, sqlite3
import struct, subprocess, sys, time, zipfile, zlib
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

NOW = datetime(2026, 10, 17, 12, 0, 0)
EMPTY_DIRS = ["Alarms", "Notifications", "Ringtones", "Podcasts", "Audiobooks", "Recordings"]

def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts if c)

def rand_dt(rng, days_back=365):
    return NOW - timedelta(seconds=rng.randint(3600, days_back * 86400))

def write(path: Path, data: bytes, dt: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = dt.timestamp()
    os.utime(path, (ts, ts))

LOREM = ("the quick brown fox jumps over the lazy dog assignment lecture notes "
         "data structures stack queue linked list tree graph traversal complexity "
         "meeting tomorrow project report submit deadline review draft budget "
         "invoice payment travel ticket booking reminder groceries recipe workout "
         "plan semester exam syllabus unit chapter summary revision").split()

def paragraph(rng, n=80):
    return " ".join(rng.choice(LOREM) for _ in range(n)).capitalize() + ".\n"

def make_jpeg(rng, w=1280, h=960):
    if HAVE_PIL:
        # small noisy gradient upscaled: fast, still a real JPEG
        sw, sh = w // 8, h // 8
        img = Image.new("RGB", (sw, sh)); px = img.load()
        c1 = [rng.randint(0, 255) for _ in range(3)]
        c2 = [rng.randint(0, 255) for _ in range(3)]
        for y in range(sh):
            for x in range(sw):
                t = (x / sw + y / sh) / 2
                px[x, y] = tuple(int(c1[i] * (1 - t) + c2[i] * t + rng.randint(-40, 40)) % 256
                                 for i in range(3))
        buf = io.BytesIO()
        img.resize((w, h), Image.BICUBIC).save(buf, "JPEG", quality=85)
        return buf.getvalue()
    hdr = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return hdr + rng.randbytes(rng.randint(150_000, 400_000)) + b"\xff\xd9"

def make_png(rng, w=1080, h=2340):
    o = rng.randint(0, 255)  # per-file variation so screenshots differ
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        band = (y // 120) % 6
        base = bytes([(40 + band * 30 + o) % 256, (80 + band * 20 + o // 2) % 256,
                      (200 - band * 25) % 256])
        raw += base * w
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))

def make_pdf(rng, title):
    text = paragraph(rng, 40).strip().replace("(", "").replace(")", "")
    stream = f"BT /F1 12 Tf 72 720 Td ({title}) Tj 0 -20 Td ({text[:90]}) Tj ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out

def make_docx(rng, title):
    body = "".join(f"<w:p><w:r><w:t>{paragraph(rng, 40).strip()}</w:t></w:r></w:p>"
                   for _ in range(rng.randint(3, 8)))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body><w:p><w:r><w:t>{title}</w:t></w:r></w:p>{body}</w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()

def make_mp3(rng, kb):
    id3 = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    return id3 + b"\xff\xfb\x90\x00" + rng.randbytes(kb * 1024)

def make_mp4(rng, kb):
    ftyp = struct.pack(">I", 24) + b"ftypisom" + struct.pack(">I", 512) + b"isomiso2"
    mdat = struct.pack(">I", kb * 1024 + 8) + b"mdat" + rng.randbytes(kb * 1024)
    return ftyp + mdat

def make_sqlite(path: Path, rng, dt):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, title TEXT, body TEXT, ts INTEGER)")
    for i in range(200):
        con.execute("INSERT INTO notes(title, body, ts) VALUES (?,?,?)",
                    (f"Note {i}", paragraph(rng, 30), int(rand_dt(rng).timestamp())))
    con.commit(); con.close()
    ts = dt.timestamp(); os.utime(path, (ts, ts))

NAMES = ["Aarav", "Diya", "Rohan", "Sneha", "Karthik", "Meera", "Vikram", "Ananya",
         "Rahul", "Priya", "Arjun", "Kavya", "Siddharth", "Nisha", "Manoj", "Divya"]
SUBJECTS = ["DST", "Maths", "DBMS", "OS", "Python", "Physics", "Digital Logic", "Java"]

def build(root: Path, rng):
    items = []

    def add(rel, data, dt, kind):
        write(root / rel, data, dt)
        items.append((rel, kind))

    for _ in range(30):
        dt = rand_dt(rng)
        add(f"DCIM/Camera/IMG_{dt:%Y%m%d_%H%M%S}.jpg", make_jpeg(rng), dt, "photo")
    for _ in range(10):
        dt = rand_dt(rng, 120)
        add(f"Pictures/Screenshots/Screenshot_{dt:%Y%m%d-%H%M%S}.png", make_png(rng), dt, "screenshot")
    for _ in range(12):
        dt = rand_dt(rng, 200)
        add(f"Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-{dt:%Y%m%d}-WA{rng.randint(0,999):04d}.jpg",
            make_jpeg(rng, 640, 480), dt, "chat_media")
    for s in SUBJECTS:
        dt = rand_dt(rng, 200)
        add(f"Documents/College/{s}_notes.docx", make_docx(rng, f"{s} notes"), dt, "docx")
        dt = rand_dt(rng, 200)
        add(f"Documents/College/{s}_assignment.pdf", make_pdf(rng, f"{s} assignment"), dt, "pdf")
    for i in range(1, 7):
        dt = rand_dt(rng, 300)
        add(f"Documents/Personal/journal_{i}.txt", "".join(paragraph(rng) for _ in range(6)).encode(), dt, "txt")
    dt = rand_dt(rng, 90)
    add("Documents/Personal/budget_2026.csv",
        ("month,income,expense\n" + "\n".join(f"2026-{m:02d},{rng.randint(8000,20000)},{rng.randint(5000,18000)}"
                                              for m in range(1, 11))).encode(), dt, "csv")
    add("Documents/Personal/passwords_DUMMY.txt",
        b"# fake credentials for lab only\nemail: lab.user@example.test / NotARealPassword1\n", dt, "txt")
    for i in range(6):
        dt = rand_dt(rng, 150)
        add(f"Download/receipt_{i+1}.pdf", make_pdf(rng, f"Receipt {i+1}"), dt, "pdf")
    for i in range(3):
        dt = rand_dt(rng, 150)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("readme.txt", paragraph(rng, 60))
            z.writestr("data.bin", rng.randbytes(20_000))
        add(f"Download/archive_{i+1}.zip", buf.getvalue(), dt, "zip")
    for i in range(8):
        dt = rand_dt(rng, 400)
        add(f"Music/track_{i+1:02d}.mp3", make_mp3(rng, rng.randint(300, 900)), dt, "mp3")
    for i in range(3):
        dt = rand_dt(rng, 300)
        add(f"Movies/clip_{i+1}.mp4", make_mp4(rng, rng.randint(800, 2000)), dt, "mp4")
    dt = rand_dt(rng, 30)
    vcf = "".join(f"BEGIN:VCARD\nVERSION:3.0\nFN:{n} {rng.choice(['K','S','R','N'])}\n"
                  f"TEL;TYPE=CELL:+91 9{rng.randint(100000000, 999999999)}\nEND:VCARD\n" for n in NAMES)
    add("Backups/contacts.vcf", vcf.encode(), dt, "vcf")
    sms = [{"from": rng.choice(NAMES), "body": paragraph(rng, 8).strip(),
            "ts": int(rand_dt(rng, 90).timestamp())} for _ in range(150)]
    add("Backups/sms_backup.json", json.dumps(sms, indent=1).encode(), dt, "json")
    sio = io.StringIO()
    w = csv.writer(sio); w.writerow(["name", "type", "duration_s", "ts"])
    for _ in range(100):
        w.writerow([rng.choice(NAMES), rng.choice(["in", "out", "missed"]),
                    rng.randint(0, 900), int(rand_dt(rng, 90).timestamp())])
    add("Backups/call_log.csv", sio.getvalue().encode(), dt, "csv")
    make_sqlite(root / "Backups/notes.db", rng, dt)
    items.append(("Backups/notes.db", "sqlite"))
    for d in EMPTY_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    return items

def make_manifest(root: Path, items, seed):
    files = []
    for rel, kind in items:
        p = root / rel
        data = p.read_bytes()
        files.append({"path": rel.replace("\\", "/"), "kind": kind, "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(),
                      "entropy": round(entropy(data[:1_000_000]), 4),
                      "mtime": int(p.stat().st_mtime)})
    man = {"seed": seed, "pillow": HAVE_PIL, "generated_for": "PS-001 lab baseline",
           "file_count": len(files), "total_bytes": sum(f["size"] for f in files), "files": files}
    (root.parent / "baseline.json").write_text(json.dumps(man, indent=1))
    return man

def adb(*args, check=True):
    return subprocess.run(["adb", *args], capture_output=True, text=True, check=check)

def push(root: Path, device_root: str):
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not any(entry.rglob("*")):
            continue  # empty dirs are created with mkdir below
        print(f"push {entry.name} -> {device_root}/")
        adb("push", str(entry), device_root + "/")
    for d in EMPTY_DIRS:
        adb("shell", "mkdir", "-p", f"{device_root}/{d}")
    adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{device_root}", check=False)
    print("push done")

def verify(manifest_path: Path, device_root: str):
    man = json.loads(manifest_path.read_text())
    changed = missing = ok = 0
    for f in man["files"]:
        dev = f"{device_root}/{f['path']}"
        r = adb("shell", f'sha256sum "{dev}"', check=False)
        out = r.stdout.strip().split()
        if r.returncode != 0 or not out:
            missing += 1; print("MISSING ", f["path"]); continue
        if out[0] != f["sha256"]:
            changed += 1; print("CHANGED ", f["path"])
        else:
            ok += 1
    print(f"verify: ok={ok} changed={changed} missing={missing}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="phone_env", help="local output dir")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--device-root", default="/sdcard")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    root = Path(a.out) / "sdcard"
    if a.verify:
        verify(Path(a.out) / "baseline.json", a.device_root); return
    rng = random.Random(a.seed)
    items = build(root, rng)
    man = make_manifest(root, items, a.seed)
    print(f"created {man['file_count']} files, {man['total_bytes']/1e6:.1f} MB in {root}")
    print(f"manifest: {Path(a.out)/'baseline.json'}  (Pillow={'yes' if HAVE_PIL else 'no'})")
    if a.push:
        push(root, a.device_root)

if __name__ == "__main__":
    main()

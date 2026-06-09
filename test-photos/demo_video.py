"""
No More Cheaters — AI Video Demo
=================================
Processes a video file frame-by-frame, detecting phones and
sideways head turns. Saves annotated output + timeline report.

Usage:  python test-photos/demo_video.py <video.mp4>
"""

import sys
from pathlib import Path
from ultralytics import YOLO
import cv2

SAMPLE_RATE = 15       # process every Nth frame
CONF_THRESHOLD = 0.25  # minimum detection confidence
SIDEWAYS_THRESHOLD = 40  # px nose-to-shoulder diff
TURNED_THRESHOLD = 80
IMG_SIZE = 640          # lower res for faster video processing

COLORS = {
    "PHONE":    (0, 0, 255),
    "SIDEWAYS": (0, 255, 255),
    "FORWARD":  (0, 255, 0),
    "BG":       (0, 0, 0),
    "TEXT":     (255, 255, 255),
}

# ── Parse args ──────────────────────────────────────────────────────────
if len(sys.argv) < 2:
    print("Usage: python test-photos/demo_video.py <video.mp4>")
    sys.exit(1)

video_path = Path(sys.argv[1])
if not video_path.is_file():
    print(f"✗ File not found: {video_path}")
    sys.exit(1)

output_dir = Path(__file__).parent / "output"
output_dir.mkdir(exist_ok=True)

# ── Open video ──────────────────────────────────────────────────────────
cap = cv2.VideoCapture(str(video_path))
if not cap.isOpened():
    print(f"✗ Could not open video: {video_path}")
    sys.exit(1)

fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
duration = total_frames / fps if fps > 0 else 0
W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

# ── Output video ────────────────────────────────────────────────────────
fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out_path = output_dir / f"{video_path.stem}_annotated.mp4"
out_fps = fps / SAMPLE_RATE
out = cv2.VideoWriter(str(out_path), fourcc, max(out_fps, 1), (W, H))

# ── Load models (nano for speed, swap to x for final) ───────────────────
print()
print("╔══════════════════════════════════════════════════════╗")
print("║      No More Cheaters — AI Video Demo v1.0          ║")
print("╚══════════════════════════════════════════════════════╝")
print()
print(f"  📁 Video:   {video_path.name}")
print(f"  ⏱  Duration: {duration:.0f}s ({total_frames} frames @ {fps:.0f}fps)")
print(f"  📐 Res:     {W}x{H}")
print(f"  🎯 Sampling: every {SAMPLE_RATE} frames → ~{total_frames//SAMPLE_RATE} frames to process")
print(f"  🧠 Model:   yolo11n (nano) — swap to yolo11x for production")
print()

print("  ⏳ Loading models...", end=" ", flush=True)
model = YOLO("yolo11n.pt")    # nano = 5MB, fast
pose = YOLO("yolo11n-pose.pt")
print("✓\n")

# ── Process frames ─────────────────────────────────────────────────────
events = []
frame_idx = 0
sampled = 0
total_sampled = total_frames // SAMPLE_RATE

while True:
    ret, frame = cap.read()
    if not ret:
        break

    if frame_idx % SAMPLE_RATE != 0:
        frame_idx += 1
        continue

    timestamp_sec = frame_idx / fps
    sampled += 1

    # --- Detection ---
    results = model(frame, imgsz=IMG_SIZE, conf=CONF_THRESHOLD, verbose=False)
    for r in results:
        for box in r.boxes:
            cls = int(box.cls[0])
            if cls != 67:
                continue
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cv2.rectangle(frame, (x1, y1), (x2, y2), COLORS["PHONE"], 2)
            text = f"PHONE {conf:.2f}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
            cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 6, y1), COLORS["PHONE"], -1)
            cv2.putText(frame, text, (x1 + 3, y1 - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLORS["TEXT"], 2)
            events.append((timestamp_sec, "PHONE_DETECTED", conf, -1))

    # --- Pose ---
    results = pose(frame, imgsz=IMG_SIZE, verbose=False)
    for i, kp in enumerate(results[0].keypoints):
        d = kp.data[0]
        nose = d[0].tolist() if d[0][2] > 0.5 else None
        l_eye = d[1].tolist() if d[1][2] > 0.5 else None
        r_eye = d[2].tolist() if d[2][2] > 0.5 else None
        ls = d[5].tolist() if d[5][2] > 0.5 else None
        rs = d[6].tolist() if d[6][2] > 0.5 else None

        if not all([nose, l_eye, r_eye, ls, rs]):
            continue

        mid_x = (ls[0] + rs[0]) / 2
        diff = abs(nose[0] - mid_x)

        if diff > TURNED_THRESHOLD:
            label = "TURNED_BACK"
            color = COLORS["SIDEWAYS"]
            events.append((timestamp_sec, "TURNED_BACK", min(diff / 100, 1.0), i))
        elif diff > SIDEWAYS_THRESHOLD:
            label = "LOOKING_SIDEWAYS"
            color = COLORS["SIDEWAYS"]
            events.append((timestamp_sec, "LOOKING_SIDEWAYS", min(diff / 100, 1.0), i))
        else:
            label = None

        if label:
            top = int(nose[1] - 60)
            bot = int((ls[1] + rs[1]) / 2 + 80)
            left = int(min(ls[0], rs[0]) - 40)
            right = int(max(ls[0], rs[0]) + 40)
            cv2.rectangle(frame, (left, top), (right, bot), color, 2)
            num_label = f"#{i+1}"
            (ntw, nth), _ = cv2.getTextSize(num_label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            cv2.rectangle(frame, (left, top + 20), (left + ntw + 8, top + 20 + nth + 8), COLORS["BG"], -1)
            cv2.putText(frame, num_label, (left + 4, top + 20 + nth + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLORS["TEXT"], 2)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
            cv2.rectangle(frame, (left, top - th - 6), (left + tw + 6, top), color, -1)
            cv2.putText(frame, label, (left + 3, top - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLORS["BG"], 2)

    # Timestamp overlay
    m, s = divmod(int(timestamp_sec), 60)
    ts_text = f"{m:02d}:{s:02d}"
    cv2.putText(frame, ts_text, (W - 110, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, COLORS["TEXT"], 2)

    out.write(frame)

    if sampled % 5 == 0:
        pct = min(sampled * 100 / total_sampled, 100)
        print(f"  ⏳ {pct:.0f}% — {sampled}/{total_sampled} frames ({len(events)} events so far)   ", end="\r", flush=True)

    frame_idx += 1

cap.release()
out.release()
print(f"\n  ✅ Done — {sampled} frames processed, {len(events)} total events")

# ── Timeline ────────────────────────────────────────────────────────────
print(f"\n  ┌─────────────── TIMELINE ───────────────┐")
phone_events = [e for e in events if e[1] == "PHONE_DETECTED"]
side_events = [e for e in events if e[1] in ("LOOKING_SIDEWAYS", "TURNED_BACK")]

for e in events[:40]:
    ts, etype, conf, pid = e
    m, s = divmod(int(ts), 60)
    icon = "📱" if etype == "PHONE_DETECTED" else "👀"
    who = f"P{pid+1}" if pid >= 0 else "  "
    print(f"  │  {m:02d}:{s:02d}  {icon} {etype:<20} {conf:.2f}  {who} │")

if len(events) > 40:
    print(f"  │  ... and {len(events) - 40} more events              │")

print(f"  └──────────────────────────────────────────┘")
print()
print(f"  ┌──────────────── SUMMARY ────────────────┐")
m, s = divmod(int(duration), 60)
print(f"  │  Duration:            {m:02d}:{s:02d}                      │")
print(f"  │  Frames sampled:      {sampled:>3} / {total_frames:<5}              │")
print(f"  │  📱 Phone events:     {len(phone_events):>3}                       │")
print(f"  │  👀 Sideways events:  {len(side_events):>3}                       │")
print(f"  └──────────────────────────────────────────┘")
print()
print(f"  💾 Saved: {out_path}")
print()

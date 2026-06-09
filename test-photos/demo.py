"""
No More Cheaters — AI Detection Demo
=====================================
Real-time exam proctoring proof-of-concept.
Flags students looking sideways (the actual cheating signal).
Saves annotated output with bounding boxes and labels.

Usage:  python test-photos/demo.py [image.png]
"""

import sys
from pathlib import Path
from ultralytics import YOLO
import cv2

IMG_SIZE = 1280
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Colour coding (BGR for OpenCV)
COLORS = {
    "PHONE":   (0, 0, 255),       # red
    "SIDEWAYS": (0, 255, 255),    # yellow
    "FORWARD": (0, 255, 0),       # green
    "LABEL":   (255, 255, 255),   # white text
    "BG":      (0, 0, 0),         # black label bg
}

TARGET_CLASSES = {67: "PHONE", 63: "LAPTOP", 73: "BOOK", 75: "REMOTE"}

# ── Pick image ──────────────────────────────────────────────────────────
if len(sys.argv) > 1:
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"✗ File not found: {path}")
        sys.exit(1)
else:
    images = sorted(Path(__file__).parent.glob("*.*"))
    images = [p for p in images if p.suffix in (".png", ".jpg", ".jpeg")]
    if not images:
        print("✗ No images found. Drop a .png/.jpg in test-photos/ first.")
        sys.exit(1)
    path = images[0]

img_bgr = cv2.imread(str(path))
if img_bgr is None:
    print(f"✗ Could not read image: {path}")
    sys.exit(1)

H, W = img_bgr.shape[:2]

# ── Banner ──────────────────────────────────────────────────────────────
print()
print("╔══════════════════════════════════════════════════════╗")
print("║         No More Cheaters — AI Demo v1.0             ║")
print("║     AI-Powered Exam Proctoring Proof of Concept     ║")
print("╚══════════════════════════════════════════════════════╝")
print()
print(f"  📁 Image:   {path.name}  ({W}x{H})")
print(f"  🧠 Models:  yolo11x (detection) + yolo11x-pose")
print()

# ── Load models ─────────────────────────────────────────────────────────
print("  ⏳ Loading models...", end=" ", flush=True)
model = YOLO("yolo11x.pt")
pose = YOLO("yolo11x-pose.pt")
print("✓\n")

# ── Detection ───────────────────────────────────────────────────────────
results = model(str(path), imgsz=IMG_SIZE)
r = results[0]

print("  ┌─────────────── DETECTION ───────────────┐")

phones = [b for b in r.boxes if int(b.cls[0]) == 67]
others = [b for b in r.boxes if int(b.cls[0]) not in TARGET_CLASSES and int(b.cls[0]) != 0]

# Scale factor if model resized
scale_x = W / IMG_SIZE if W > IMG_SIZE else 1
scale_y = H / IMG_SIZE if H > IMG_SIZE else 1

for b in phones:
    cls = int(b.cls[0])
    conf = float(b.conf[0])
    x1, y1, x2, y2 = map(int, b.xyxy[0])

    if scale_x != 1:
        x1, x2 = int(x1 * scale_x), int(x2 * scale_x)
    if scale_y != 1:
        y1, y2 = int(y1 * scale_y), int(y2 * scale_y)

    # Draw red box
    cv2.rectangle(img_bgr, (x1, y1), (x2, y2), COLORS["PHONE"], 3)

    # Label background + text
    text = f"PHONE {conf:.2f}"
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    cv2.rectangle(img_bgr, (x1, y1 - th - 8), (x1 + tw + 8, y1), COLORS["PHONE"], -1)
    cv2.putText(img_bgr, text, (x1 + 4, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.7, COLORS["LABEL"], 2)

    print(f"  │  📱 PHONE              conf {conf:.2f}         │")

if others:
    print(f"  │  ── other objects ──                  │")
    for b in others:
        name = model.names[int(b.cls[0])]
        print(f"  │     {name:<20}  conf {b.conf[0]:.2f}     │")

if not phones:
    print(f"  │  No phones detected                   │")

print(f"  └──────────────────────────────────────────┘\n")

# ── Pose ────────────────────────────────────────────────────────────────
results = pose(str(path), imgsz=IMG_SIZE)
r2 = results[0]

print("  ┌─────────────── HEAD ORIENTATION ─────────┐")

person_data = []
for i, kp in enumerate(r2.keypoints):
    d = kp.data[0]
    nose = d[0].tolist() if d[0][2] > 0.5 else None
    l_eye = d[1].tolist() if d[1][2] > 0.5 else None
    r_eye = d[2].tolist() if d[2][2] > 0.5 else None
    l_shoulder = d[5].tolist() if d[5][2] > 0.5 else None
    r_shoulder = d[6].tolist() if d[6][2] > 0.5 else None

    if not all([nose, l_eye, r_eye, l_shoulder, r_shoulder]):
        print(f"  │  Person {i+1}: ⚠ weak keypoints             │")
        person_data.append(("unknown", None))
        continue

    # Head direction: where is the nose relative to shoulder center?
    shoulder_mid_x = (l_shoulder[0] + r_shoulder[0]) / 2
    nose_to_shoulder = nose[0] - shoulder_mid_x

    # Also check which way eyes are looking relative to nose
    eye_mid_x = (l_eye[0] + r_eye[0]) / 2
    eye_to_side = nose[0] - eye_mid_x  # positive = nose right of eyes (head turned left)

    # Combined score: how far the nose has shifted from center
    horiz_diff = abs(nose_to_shoulder)

    # Determine severity
    if horiz_diff > 80:
        status = "🔄 HEAD TURNED BACK"
        box_color = COLORS["SIDEWAYS"]
        label = "TURNED_BACK"
        flagged = True
    elif horiz_diff > 40:
        status = "👀 LOOKING SIDEWAYS"
        box_color = COLORS["SIDEWAYS"]
        label = "LOOKING_SIDEWAYS"
        flagged = True
    else:
        status = "✅ FACING FORWARD"
        box_color = COLORS["FORWARD"]
        label = "FACING_FORWARD"
        flagged = False

    person_data.append((status, flagged))

    # Draw person bounding box (estimated from shoulders + head)
    person_top = int(nose[1] - 60)
    person_bot = int((l_shoulder[1] + r_shoulder[1]) / 2 + 80)
    person_left = int(min(l_shoulder[0], r_shoulder[0]) - 40)
    person_right = int(max(l_shoulder[0], r_shoulder[0]) + 40)

    # Scale
    if scale_x != 1:
        person_left, person_right = int(person_left * scale_x), int(person_right * scale_x)
    if scale_y != 1:
        person_top, person_bot = int(person_top * scale_y), int(person_bot * scale_y)

    cv2.rectangle(img_bgr, (person_left, person_top), (person_right, person_bot), box_color, 2)

    # Person number badge (top-left corner of box)
    num_label = f"#{i+1}"
    (ntw, nth), _ = cv2.getTextSize(num_label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 3)
    badge_x, badge_y = person_left, person_top + 30
    cv2.rectangle(img_bgr, (badge_x, badge_y), (badge_x + ntw + 10, badge_y + nth + 10), (0, 0, 0), -1)
    cv2.putText(img_bgr, num_label, (badge_x + 5, badge_y + nth + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 3)

    # Status label above the box
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    cv2.rectangle(img_bgr, (person_left, person_top - th - 8), (person_left + tw + 8, person_top), box_color, -1)
    cv2.putText(img_bgr, label, (person_left + 4, person_top - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLORS["BG"], 2)

    print(f"  │  Person {i+1}: {status:<23} │")

print(f"  └──────────────────────────────────────────┘\n")

# ── Save annotated image ────────────────────────────────────────────────
output_path = OUTPUT_DIR / f"{path.stem}_annotated{path.suffix}"
cv2.imwrite(str(output_path), img_bgr)
print(f"  💾 Saved: {output_path}")

# ── Summary ─────────────────────────────────────────────────────────────
flagged_count = sum(1 for p in person_data if p[1])
total_people = len(person_data)

print()
print("  ┌──────────────── SUMMARY ────────────────┐")
print(f"  │  People detected:            {total_people:>2}           │")
print(f"  │  Phones found:               {len(phones):>2}           │")
print(f"  │  🚩 Looking sideways/back:   {flagged_count:>2}/{total_people:>2}           │")
print(f"  │  Inference time:          {r.speed['inference']/1000:.1f}s        │")
print("  └──────────────────────────────────────────┘")
print()
print("  " + "═" * 50)
print("  Demo complete. All analysis is local — no data leaves your machine.")
print()

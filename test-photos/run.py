"""Run YOLO detection + pose on images in test-photos/.
Flags phones and sideways/backward head turns only.

Usage:
    python test-photos/run.py                          # first image
    python test-photos/run.py test-photos/photo.png    # specific file
    python test-photos/run.py test-photos/             # all images
"""
import sys
from pathlib import Path
from ultralytics import YOLO

IMG_SIZE = 1280
TARGET_CLASSES = {67: "📱 PHONE", 63: "💻 LAPTOP", 73: "📖 BOOK", 75: "📟 REMOTE"}

# --- Gather images ---
if len(sys.argv) > 1:
    source = sys.argv[1]
    path = Path(source)
    if path.is_dir():
        images = sorted(p for p in path.iterdir() if p.suffix in (".png", ".jpg", ".jpeg"))
    elif path.is_file():
        images = [path]
    else:
        print(f"Not found: {source}")
        sys.exit(1)
else:
    folder = Path(__file__).parent
    images = sorted(p for p in folder.iterdir() if p.suffix in (".png", ".jpg", ".jpeg"))

if not images:
    print("No images found. Drop .png/.jpg files in test-photos/ and try again.")
    sys.exit(1)

# --- Load models once ---
print("Loading models...", end=" ", flush=True)
model = YOLO("yolo11x.pt")
pose = YOLO("yolo11x-pose.pt")
print("done\n")

for img_path in images:
    print(f"\n{'='*50}")
    print(f"  File: {img_path.name}")
    print(f"{'='*50}")

    # --- Detection ---
    results = model(str(img_path), imgsz=IMG_SIZE)
    phones = []
    others = []
    for r in results:
        for box in r.boxes:
            cls = int(box.cls[0])
            conf = float(box.conf[0])
            name = model.names[cls]
            if cls in TARGET_CLASSES:
                phones.append(f"{TARGET_CLASSES[cls]} — conf {conf:.2f}")
            elif cls != 0:
                others.append(f"{name} — conf {conf:.2f}")

    print(f"\n  📦 Objects:")
    if phones:
        for p in phones:
            print(f"     {p}")
    else:
        print(f"     (no phones / suspicious objects)")
    if others:
        for o in others:
            print(f"     ({o})")

    # --- Pose (sideways only) ---
    results = pose(str(img_path), imgsz=IMG_SIZE)
    people = results[0].keypoints
    print(f"\n  🧍 People: {len(people)}")

    for i, kp in enumerate(people):
        d = kp.data[0]
        nose = d[0].tolist() if d[0][2] >= 0.5 else None
        left_eye = d[1].tolist() if d[1][2] >= 0.5 else None
        right_eye = d[2].tolist() if d[2][2] >= 0.5 else None
        l_shoulder = d[5].tolist() if d[5][2] >= 0.5 else None
        r_shoulder = d[6].tolist() if d[6][2] >= 0.5 else None

        if not all([nose, left_eye, right_eye, l_shoulder, r_shoulder]):
            print(f"     Person {i+1}: ⚠ weak keypoints")
            continue

        shoulder_mid_x = (l_shoulder[0] + r_shoulder[0]) / 2
        horiz_diff = abs(nose[0] - shoulder_mid_x)

        if horiz_diff > 80:
            print(f"     Person {i+1}: 🔄 HEAD TURNED BACK  (diff: {horiz_diff:.0f}px)")
        elif horiz_diff > 40:
            print(f"     Person {i+1}: 👀 LOOKING SIDEWAYS  (diff: {horiz_diff:.0f}px)")
        else:
            print(f"     Person {i+1}: ✅ FACING FORWARD    (diff: {horiz_diff:.0f}px)")

print(f"\n{'='*50}")
print("  Done.")

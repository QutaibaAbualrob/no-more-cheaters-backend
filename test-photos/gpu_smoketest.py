"""Local GPU smoke test for the YOLO pipeline.

Exercises the REAL pipeline detectors (``ObjectDetector`` + ``PoseAnalyzer``, the
shared model cache, and ``config.resolve_device``) on one image, so it confirms
the actual GPU code path — device selection, FP16 gating, model warmup/cache —
not merely that torch can see a GPU. Prints the resolved device, whether FP16
engaged, per-model steady-state timing, and peak VRAM with both models resident.

Re-runnable on the AWS T4 to confirm the same path there (expect higher latency
than a desktop Ampere card, lower VRAM pressure on the 16 GB card).

Usage (from no-more-cheaters-backend/):
    python test-photos/gpu_smoketest.py [image_path]
Defaults to bus.jpg in the backend root.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

import cv2  # noqa: E402

from nomorecheaters.apis.ai import config  # noqa: E402
from nomorecheaters.apis.ai.yolo_detector import ObjectDetector  # noqa: E402
from nomorecheaters.apis.ai.pose_analyzer import PoseAnalyzer  # noqa: E402


def main() -> int:
    img_path = sys.argv[1] if len(sys.argv) > 1 else str(BACKEND_ROOT / 'bus.jpg')
    frame = cv2.imread(img_path)
    if frame is None:
        print(f'Could not read image: {img_path}')
        return 1

    import torch

    device = config.resolve_device()
    on_gpu = device != 'cpu'

    print(f'image            : {img_path}  {frame.shape[1]}x{frame.shape[0]}')
    print(f'torch            : {torch.__version__} | cuda build {torch.version.cuda} '
          f'| available {torch.cuda.is_available()}')
    print(f'resolved device  : {device!r}  ({"GPU" if on_gpu else "CPU"})')
    if on_gpu:
        print(f'gpu              : {torch.cuda.get_device_name(0)}')
        torch.cuda.reset_peak_memory_stats()
    print(f'imgsz            : object {config.OBJECT_IMGSZ} / pose {config.POSE_IMGSZ}')
    print(f'FP16 (half)      : object={config.OBJECT_HALF} pose={config.POSE_HALF} '
          f'-> applied={on_gpu and config.OBJECT_HALF}')

    obj = ObjectDetector()
    pose = PoseAnalyzer()

    # First call triggers lazy load + one-shot warmup (cached). Time a SECOND call
    # for steady-state latency (cuDNN already autotuned for this input shape).
    obj.detect(frame)
    pose.analyze_frame(frame)

    t0 = time.perf_counter()
    dets = obj.detect(frame)
    t_obj = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    pose_dets, person_boxes = pose.analyze_frame(frame)
    t_pose = (time.perf_counter() - t0) * 1000.0

    print(f'object detect    : {t_obj:6.1f} ms  ({len(dets)} kept phone/laptop)')
    print(f'pose analyze     : {t_pose:6.1f} ms  ({len(person_boxes)} persons, '
          f'{len(pose_dets)} looking-away)')
    if on_gpu:
        peak_res = torch.cuda.max_memory_reserved() / 1e9
        peak_alloc = torch.cuda.max_memory_allocated() / 1e9
        print(f'peak VRAM        : reserved {peak_res:.2f} GB | allocated {peak_alloc:.2f} GB '
              f'(both yolo11x models resident)')
        print('OK - GPU pipeline path exercised (FP16 on GPU, cache + warmup).')
    else:
        print('NOTE - ran on CPU. Install a CUDA torch to exercise the GPU path.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

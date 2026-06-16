"""Configuration for the YOLO-based video analysis pipeline.

Values are read from environment variables with sensible defaults so the
package stays importable and unit-testable without Django configured. The
behaviour identifiers below MUST match ``apis.models.Alert.BehaviorType``
values — they are the contract between this module and the database layer
(wired up in Task 1.4 / 1.5).
"""

from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)


# --- Behaviour identifiers (must mirror apis.models.Alert.BehaviorType) ------
PHONE_DETECTED = 'PHONE_DETECTED'
LAPTOPS = 'LAPTOPS'
LOOKING_AWAY = 'LOOKING_AWAY'

# COCO class id -> behaviour type for the object detector. Only these two
# classes are kept; every other YOLO detection is ignored.
COCO_CLASS_MAP = {
    67: PHONE_DETECTED,  # COCO "cell phone"
    63: LAPTOPS,         # COCO "laptop"
}

# Direct physical-evidence behaviours: a forbidden object actually visible in
# frame. Unlike the soft, jittery looking-away pose cue, even a brief sighting
# is a real violation, so these bypass the "sustained duration" noise filter in
# the temporal layer (see ``consolidate_events``). Without this, a phone shown
# for only a couple of sampled frames would be discarded and never reported.
DIRECT_EVIDENCE_BEHAVIORS = frozenset({PHONE_DETECTED, LAPTOPS})

# BGR colours used when drawing annotations onto the output video.
BEHAVIOR_COLORS = {
    PHONE_DETECTED: (0, 0, 255),    # red
    LAPTOPS: (0, 165, 255),         # orange
    LOOKING_AWAY: (0, 215, 255),    # amber
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Model weights -----------------------------------------------------------
# ultralytics resolves these names and downloads the weights on first use
# (~220MB total), caching them on disk for subsequent runs.
OBJECT_MODEL = os.getenv('AI_OBJECT_MODEL', 'yolo11x.pt')
POSE_MODEL = os.getenv('AI_POSE_MODEL', 'yolo11x-pose.pt')

# --- Inference device --------------------------------------------------------
# AI_DEVICE controls where YOLO runs:
#   "auto" (default) → CUDA GPU (device 0) when available, else CPU
#   "cpu"            → force CPU
#   "0" / "cuda"     → force the first CUDA GPU
AI_DEVICE = os.getenv('AI_DEVICE', 'auto')

_resolved_device = None


def resolve_device():
    """Return the ultralytics device arg: ``0`` for GPU, ``'cpu'`` otherwise.

    Honours ``AI_DEVICE`` ("auto"/"cpu"/"0"/"cuda"). For "auto", CUDA is used
    when ``torch.cuda.is_available()``. Resolved once and cached; the chosen
    device is logged on first resolution. A missing/broken torch falls back to
    CPU so analysis never crashes on the device check.
    """
    global _resolved_device
    if _resolved_device is not None:
        return _resolved_device

    raw = (AI_DEVICE or 'auto').strip().lower()
    if raw == 'cpu':
        device = 'cpu'
    elif raw in ('0', 'cuda', 'cuda:0', 'gpu'):
        device = 0
    else:  # "auto" (or anything unrecognised) → decide at runtime
        try:
            import torch

            device = 0 if torch.cuda.is_available() else 'cpu'
        except Exception:  # noqa: BLE001 — torch missing/broken → safe CPU fallback
            device = 'cpu'

    _resolved_device = device
    logger.info('Running YOLO on device: %s', device)
    return device

# --- Frame sampling ----------------------------------------------------------
# Analyse every Nth frame. At ~30fps the default samples roughly twice/second.
SAMPLE_EVERY_N_FRAMES = max(1, _env_int('AI_SAMPLE_RATE', 15))

# --- Confidence thresholds ---------------------------------------------------
OBJECT_CONFIDENCE = _env_float('AI_OBJECT_CONFIDENCE', 0.40)
# Minimum person/keypoint confidence required before head-pose is trusted.
POSE_CONFIDENCE = _env_float('AI_POSE_CONFIDENCE', 0.50)
# Nose offset from the eye midpoint, as a fraction of inter-eye distance,
# beyond which the head is considered "turned away".
LOOKING_AWAY_RATIO = _env_float('AI_LOOKING_AWAY_RATIO', 0.35)

# --- Temporal rules layer ----------------------------------------------------
# Detections of the same class within this sliding window merge into one event.
MERGE_WINDOW_SEC = _env_float('AI_MERGE_WINDOW_SEC', 5.0)
# Consolidated events shorter than this are dropped as noise (a sustained
# detection is required before anything is reported).
MIN_EVENT_DURATION_SEC = _env_float('AI_MIN_EVENT_SEC', 2.0)

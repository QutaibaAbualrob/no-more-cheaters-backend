"""Configuration for the YOLO-based video analysis pipeline.

Values are read from environment variables with sensible defaults so the
package stays importable and unit-testable without Django configured. The
behaviour identifiers below MUST match ``apis.models.Alert.BehaviorType``
values — they are the contract between this module and the database layer
(wired up in Task 1.4 / 1.5).
"""

from __future__ import annotations

import os


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

# Inference device. '' lets ultralytics auto-select (CUDA when available, else
# CPU). Override with e.g. 'cpu', 'cuda:0'.
DEVICE = os.getenv('AI_DEVICE', '')

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

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    value = raw.strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    return default


# --- Model weights -----------------------------------------------------------
# ultralytics resolves these names and downloads the weights on first use
# (~220MB total), caching them on disk for subsequent runs.
OBJECT_MODEL = os.getenv('AI_OBJECT_MODEL', 'yolo11x.pt')
POSE_MODEL = os.getenv('AI_POSE_MODEL', 'yolo11x-pose.pt')

# --- Inference resolution ----------------------------------------------------
# Source recordings are typically 1280x720. The ultralytics default (imgsz=640)
# downscales the frame ~2x linearly, which destroys the few pixels a small,
# distant phone occupies — so phones are essentially never detected at 640.
# 960 keeps far more of that detail at ~2-2.5x the compute of 640; bump to 1280
# for the best small-object recall (~3.5-4x). Box coordinates are unaffected:
# ultralytics rescales every box back to ORIGINAL source-frame pixels regardless
# of imgsz.
OBJECT_IMGSZ = max(32, _env_int('AI_OBJECT_IMGSZ', 960))
POSE_IMGSZ = max(32, _env_int('AI_POSE_IMGSZ', 960))
# FP16 inference — GPU-only (errors on CPU), passed to predict() only when the
# resolved device is not the CPU. Opt-in.
OBJECT_HALF = _env_bool('AI_OBJECT_HALF', False)
# Class-agnostic NMS. Near-inert because the detector is restricted to classes
# {phone, laptop}; opt-in only, default off.
OBJECT_AGNOSTIC_NMS = _env_bool('AI_AGNOSTIC_NMS', False)

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
# OBJECT_CONFIDENCE is the coarse predict() floor (in the live pipeline it is
# overridden by the instructor's AIThresholds sensitivity — see
# services.build_ai_report). On TOP of that floor each class has its own keep
# threshold (CLASS_CONFIDENCE), applied post-inference in the detector as
# max(floor, class_keep):
#   * LAPTOP is kept HIGH (0.85) because YOLO reads bright rectangular exam
#     paper/books as "laptop" with confidence reaching ~0.79; 0.85 rejects them
#     regardless of the slider. This is the paper-false-positive fix.
#   * PHONE keeps the floor (>=0.30 minimum) — phones are the real target and
#     are hard to see, so recall is governed by the slider + imgsz, not a high
#     per-class bar.
OBJECT_CONFIDENCE = _env_float('AI_OBJECT_CONFIDENCE', 0.40)
CLASS_CONFIDENCE = {
    PHONE_DETECTED: _env_float('AI_PHONE_CONFIDENCE', 0.30),
    LAPTOPS: _env_float('AI_LAPTOP_CONFIDENCE', 0.85),
}
# Minimum person/keypoint confidence required before head-pose is trusted.
POSE_CONFIDENCE = _env_float('AI_POSE_CONFIDENCE', 0.50)
# Nose offset from the eye midpoint, as a fraction of inter-eye distance.
# Retained as a low-level input to the head-yaw estimate below.
LOOKING_AWAY_RATIO = _env_float('AI_LOOKING_AWAY_RATIO', 0.35)

# --- Head-pose (looking-away) gating -----------------------------------------
# A person is only flagged LOOKING_AWAY when their head is turned SIDEWAYS by
# more than this many degrees. Looking *down* (a vertical pitch — normal exam
# behaviour) is never flagged because the estimate below is horizontal-only.
LOOKING_AWAY_ANGLE_DEG = _env_float('AI_HEAD_TURN_ANGLE_DEG', 45.0)
# Approximate nose-protrusion-to-inter-ocular-distance ratio used to convert the
# 2D horizontal nose offset into a yaw angle: yaw ≈ atan(offset_ratio / this).
# ~0.55 is a typical adult-face value; it is an approximation from 2D keypoints,
# not a true 3D pose solve. Lower → the same offset reads as a larger angle.
NOSE_DEPTH_RATIO = _env_float('AI_NOSE_DEPTH_RATIO', 0.55)

# --- Head-pose (looking-away) v2 heuristic -----------------------------------
# Replaces the brittle nose-vs-eye-midpoint yaw (above, kept for back-compat).
# Up to three votes; LOOKING_AWAY is flagged only when at least
# LOOKING_AWAY_MIN_VOTES fire AND the mandatory horizontal-offset vote (V2) is
# among them — so a purely vertical pitch (looking down to write) never flags.
#   V1  ear asymmetry — only one ear confidently visible (a profile); the
#       visible-ear side also gives turn direction.
#   V2  horizontal offset (MANDATORY) — nose displaced sideways from the
#       shoulder midpoint, normalised by shoulder width (scale-invariant), so it
#       reads pure yaw and ignores pitch.
#   V3  corroborating — a horizontal offset large enough to be self-evident.
EAR_VISIBLE_CONF = _env_float('AI_EAR_VISIBLE_CONF', 0.60)
EAR_HIDDEN_CONF = _env_float('AI_EAR_HIDDEN_CONF', 0.35)
EAR_CONF_RATIO = _env_float('AI_EAR_CONF_RATIO', 2.5)
NOSE_SHOULDER_SIDEWAYS = _env_float('AI_NOSE_SHOULDER_SIDEWAYS', 0.35)
NOSE_SHOULDER_TURNED = _env_float('AI_NOSE_SHOULDER_TURNED', 0.55)
LOOKING_AWAY_MIN_VOTES = max(1, _env_int('AI_LOOKING_AWAY_VOTES', 2))

# Master switch for looking-away detection. DEFAULT OFF.
#
# The heuristic is sound for a FRONTAL (student-facing) camera, but NOT for an
# oblique/ceiling/wide camera: perspective alone displaces the nose horizontally
# and hides one ear for *forward-facing* students, so the whole class trips it
# (this footage flagged 7 of 12 forward/down-facing students). Enable it
# (AI_ENABLE_LOOKING_AWAY=true) only with a roughly frontal camera; for an
# oblique camera a dedicated 3D head-pose model (6DRepNet / L2CS-Net) is the
# proper path. The pose pass still runs when this is off (it harvests the YOLO
# person boxes used for evidence, H6) — only the looking-away *alerts* are
# suppressed.
ENABLE_LOOKING_AWAY = _env_bool('AI_ENABLE_LOOKING_AWAY', False)

# A looking-away event must contain at least this many *consecutive* sampled
# frames before it becomes an alert — a single momentary glance never counts.
LOOKING_AWAY_MIN_CONSECUTIVE = max(1, _env_int('AI_CONSECUTIVE_DETECTIONS', 3))

# --- Temporal rules layer ----------------------------------------------------
# Detections of the same class within this sliding window merge into one event.
MERGE_WINDOW_SEC = _env_float('AI_MERGE_WINDOW_SEC', 5.0)
# Consolidated events shorter than this are dropped as noise (a sustained
# detection is required before anything is reported).
MIN_EVENT_DURATION_SEC = _env_float('AI_MIN_EVENT_SEC', 2.0)

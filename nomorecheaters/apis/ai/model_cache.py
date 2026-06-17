"""Process-level cache + warmup for loaded YOLO models.

A single serialized analysis worker processes one video at a time but many jobs
over its lifetime. Loading the ~115 MB YOLO11x weights and initialising the CUDA
context on every job is pure overhead, so each model is loaded once per
``(weights, device)`` and reused across jobs. On first load a one-shot dummy
inference warms the model (CUDA context, cuDNN autotune, weight upload) so the
first real frame of the first job isn't slow.

Reuse is safe ONLY because inference is serialized — one video, and within it one
frame, at a time. Sharing a single CUDA model across concurrent threads is
fragile and is deliberately avoided in the pipeline (see
``detector._collect_frame_detections``). Run a single ``rqworker`` per GPU.
"""

from __future__ import annotations

import logging
import threading

from . import config

logger = logging.getLogger(__name__)

# (weights_path, str(device)) -> loaded ultralytics YOLO model, kept resident for
# the life of the worker process so successive jobs reuse it.
_CACHE: dict = {}
_LOCK = threading.Lock()


def load_model(
    model_path: str,
    device,
    *,
    half: bool = False,
    warmup_imgsz: int | None = None,
):
    """Return a cached YOLO model for ``(model_path, device)``, loading it once.

    On first load the weights are read (ultralytics downloads them if not already
    cached on disk), moved to *device*, and — when :data:`config.WARMUP` is set
    and *warmup_imgsz* is given — warmed with a dummy inference. Heavy imports
    (ultralytics, numpy) stay local so importing this module is cheap and unit
    tests that never touch a real model don't pay for them.
    """
    key = (model_path, str(device))
    with _LOCK:
        model = _CACHE.get(key)
        if model is not None:
            return model

        from ultralytics import YOLO

        model = YOLO(model_path)
        model.to(device)
        _CACHE[key] = model
        logger.info('Loaded YOLO weights %s onto device %s', model_path, device)

        if config.WARMUP and warmup_imgsz:
            _warmup(model, device, warmup_imgsz, half)
        return model


def _warmup(model, device, imgsz: int, half: bool) -> None:
    """Best-effort dummy inference so the first real frame isn't cold."""
    try:
        import numpy as np

        blank = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
        kwargs = {'verbose': False, 'device': device, 'imgsz': imgsz}
        # Mirror the detectors' GPU-only FP16 gate so the warmed kernels match the
        # precision the real frames will use (FP16 is a GPU-only optimization).
        if half and device != 'cpu':
            kwargs['half'] = True
        model(blank, **kwargs)
        logger.info('Warmed up YOLO model (device %s, imgsz %s)', device, imgsz)
    except Exception as exc:  # noqa: BLE001 — warmup must never block analysis
        logger.warning('YOLO warmup skipped (%s)', exc)


def clear() -> None:
    """Drop all cached models (frees VRAM). Mainly for tests."""
    with _LOCK:
        _CACHE.clear()

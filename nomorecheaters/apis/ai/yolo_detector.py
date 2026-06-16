"""Object detection with YOLO11x.

Loads the pretrained ``yolo11x.pt`` model and runs inference on individual
frames, keeping only the two COCO classes the proctoring pipeline cares about:
cell phones (-> ``PHONE_DETECTED``) and laptops (-> ``LAPTOPS``). The model is
loaded lazily on first use, which is also when ultralytics downloads the
weights if they are not already cached.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass
class Detection:
    """A single kept object detection within one frame."""

    behavior_type: str
    confidence: float
    bbox: tuple  # (x1, y1, x2, y2) in pixel coordinates


class ObjectDetector:
    """Thin wrapper around a YOLO11x model scoped to phone/laptop classes."""

    def __init__(
        self,
        model_path: str = config.OBJECT_MODEL,
        confidence: float = config.OBJECT_CONFIDENCE,
        device=None,
        class_map: dict | None = None,
        imgsz: int = config.OBJECT_IMGSZ,
        half: bool = config.OBJECT_HALF,
        agnostic_nms: bool = config.OBJECT_AGNOSTIC_NMS,
        class_confidence: dict | None = None,
    ):
        self.model_path = model_path
        # `confidence` is the coarse predict() floor. Each class additionally has
        # its own (higher) keep threshold in `class_confidence`, applied below as
        # max(floor, class_keep).
        self.confidence = confidence
        self.imgsz = imgsz
        self.half = half
        self.agnostic_nms = agnostic_nms
        self.class_confidence = dict(
            class_confidence if class_confidence is not None else config.CLASS_CONFIDENCE
        )
        # Resolved lazily here (analysis time), so importing the package never
        # touches torch/CUDA. `0` means GPU; `'cpu'` means CPU.
        self.device = device if device is not None else config.resolve_device()
        self.class_map = class_map if class_map is not None else config.COCO_CLASS_MAP
        self._model = None

    @property
    def model(self):
        """Lazily load (and on first run, download) the YOLO weights onto the device."""
        if self._model is None:
            from ultralytics import YOLO

            self._model = YOLO(self.model_path)
            self._model.to(self.device)
        return self._model

    def detect(self, frame) -> list[Detection]:
        """Return the kept phone/laptop detections in a single BGR frame."""
        target_classes = list(self.class_map.keys())
        # Pass device explicitly every time. Note `self.device` may be `0` (GPU),
        # which is falsy — so this must NOT be gated behind `if self.device`.
        predict_kwargs = {
            'conf': self.confidence,
            'classes': target_classes,
            'imgsz': self.imgsz,
            'agnostic_nms': self.agnostic_nms,
            'verbose': False,
            'device': self.device,
        }
        # FP16 only works on GPU; passing half=True on CPU raises. Gate on the
        # resolved device, NOT on `if self.half` alone (device 0 is falsy).
        if self.half and self.device != 'cpu':
            predict_kwargs['half'] = True

        results = self.model(frame, **predict_kwargs)

        detections: list[Detection] = []
        for result in results:
            boxes = getattr(result, 'boxes', None)
            if boxes is None:
                continue
            for box in boxes:
                class_id = int(box.cls[0])
                behavior_type = self.class_map.get(class_id)
                if behavior_type is None:
                    continue
                confidence = float(box.conf[0])
                # Per-class keep threshold on TOP of the predict floor: a laptop
                # must clear a high bar (rejecting paper false positives) while a
                # phone keeps the floor. max() so the slider-driven floor still
                # applies when it is higher than the per-class minimum.
                keep_threshold = max(
                    self.confidence, self.class_confidence.get(behavior_type, 0.0)
                )
                if confidence < keep_threshold:
                    continue
                # ultralytics rescales box.xyxy back to ORIGINAL source-frame
                # pixels regardless of imgsz, so this bbox is directly drawable.
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                detections.append(
                    Detection(
                        behavior_type=behavior_type,
                        confidence=confidence,
                        bbox=(x1, y1, x2, y2),
                    )
                )
        return detections

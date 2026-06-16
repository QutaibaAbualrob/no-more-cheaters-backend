"""Head-pose estimation with YOLO11x-pose.

Loads the pretrained ``yolo11x-pose.pt`` model, extracts body keypoints for
each detected person, and uses a simple geometric heuristic to decide whether a
person is looking away from the screen: the horizontal offset of the nose from
the midpoint between the eyes, normalised by the inter-eye distance. A large
offset means the head is turned.

This is intentionally heuristic, not a trained gaze model. Momentary turns are
expected; the temporal-rules layer in :mod:`apis.ai.detector` is what enforces
that a ``LOOKING_AWAY`` posture must be *sustained* before it becomes an event.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config

# COCO pose keypoint indices used by the heuristic.
_NOSE = 0
_LEFT_EYE = 1
_RIGHT_EYE = 2


@dataclass
class PoseDetection:
    """A per-frame looking-away flag for one person."""

    behavior_type: str
    confidence: float
    bbox: tuple  # person bounding box (x1, y1, x2, y2)


class PoseAnalyzer:
    """Wrapper around YOLO11x-pose that flags looking-away postures."""

    def __init__(
        self,
        model_path: str = config.POSE_MODEL,
        keypoint_confidence: float = config.POSE_CONFIDENCE,
        looking_away_ratio: float = config.LOOKING_AWAY_RATIO,
        device=None,
    ):
        self.model_path = model_path
        self.keypoint_confidence = keypoint_confidence
        self.looking_away_ratio = looking_away_ratio
        # `0` means GPU; `'cpu'` means CPU. Resolved at analysis time.
        self.device = device if device is not None else config.resolve_device()
        self._model = None

    @property
    def model(self):
        """Lazily load (and on first run, download) the pose weights onto the device."""
        if self._model is None:
            from ultralytics import YOLO

            self._model = YOLO(self.model_path)
            self._model.to(self.device)
        return self._model

    def analyze(self, frame) -> list[PoseDetection]:
        """Return a looking-away flag per person whose head is turned."""
        # Pass device explicitly; `0` (GPU) is falsy so it must not be gated.
        predict_kwargs = {'verbose': False, 'device': self.device}

        results = self.model(frame, **predict_kwargs)

        detections: list[PoseDetection] = []
        for result in results:
            keypoints = getattr(result, 'keypoints', None)
            if keypoints is None or keypoints.xy is None:
                continue

            xy = keypoints.xy  # (persons, 17, 2)
            conf = getattr(keypoints, 'conf', None)  # (persons, 17) or None
            boxes = getattr(result, 'boxes', None)

            for person_idx in range(len(xy)):
                person_kpts = xy[person_idx]
                if len(person_kpts) <= _RIGHT_EYE:
                    continue

                if conf is not None:
                    kpt_conf = conf[person_idx]
                    if min(
                        float(kpt_conf[_NOSE]),
                        float(kpt_conf[_LEFT_EYE]),
                        float(kpt_conf[_RIGHT_EYE]),
                    ) < self.keypoint_confidence:
                        continue

                nose_x = float(person_kpts[_NOSE][0])
                left_eye_x = float(person_kpts[_LEFT_EYE][0])
                right_eye_x = float(person_kpts[_RIGHT_EYE][0])

                eye_distance = abs(left_eye_x - right_eye_x)
                if eye_distance < 1e-3:
                    continue  # eyes coincident/undetected — can't judge pose

                eye_midpoint_x = (left_eye_x + right_eye_x) / 2.0
                offset_ratio = abs(nose_x - eye_midpoint_x) / eye_distance
                if offset_ratio <= self.looking_away_ratio:
                    continue

                bbox = (0.0, 0.0, 0.0, 0.0)
                if boxes is not None and person_idx < len(boxes):
                    bbox = tuple(
                        float(v) for v in boxes[person_idx].xyxy[0].tolist()
                    )

                # Map the offset onto a bounded confidence score: at the
                # threshold it starts near 0, saturating toward 1.0 as the head
                # turns further.
                confidence = max(
                    0.0,
                    min(
                        1.0,
                        (offset_ratio - self.looking_away_ratio)
                        / max(self.looking_away_ratio, 1e-3),
                    ),
                )
                detections.append(
                    PoseDetection(
                        behavior_type=config.LOOKING_AWAY,
                        confidence=confidence,
                        bbox=bbox,
                    )
                )
        return detections

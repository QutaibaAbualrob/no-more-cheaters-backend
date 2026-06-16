"""Head-pose estimation with YOLO11x-pose.

Loads the pretrained ``yolo11x-pose.pt`` model, extracts body keypoints for
each detected person, and estimates how far the head is turned **sideways**
(yaw) from the screen. The horizontal offset of the nose from the midpoint
between the eyes (normalised by the inter-eye distance) is converted into an
approximate yaw angle; a person is flagged ``LOOKING_AWAY`` only when that angle
exceeds :data:`config.LOOKING_AWAY_ANGLE_DEG` (45° by default).

Because the estimate is purely horizontal, looking *down* (a vertical pitch —
normal exam behaviour while writing) never triggers a flag. The angle is an
approximation derived from 2D keypoints, not a true 3D pose solve.

This is intentionally heuristic, not a trained gaze model. Momentary turns are
expected; the temporal-rules layer in :mod:`apis.ai.detector` additionally
requires several *consecutive* sampled frames before a posture becomes an event.
"""

from __future__ import annotations

import math
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
        angle_threshold_deg: float = config.LOOKING_AWAY_ANGLE_DEG,
        nose_depth_ratio: float = config.NOSE_DEPTH_RATIO,
        device=None,
    ):
        self.model_path = model_path
        self.keypoint_confidence = keypoint_confidence
        # Head must be yawed past this many degrees sideways to count as away.
        self.angle_threshold_deg = angle_threshold_deg
        self.nose_depth_ratio = max(1e-3, nose_depth_ratio)
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
        return self.analyze_frame(frame)[0]

    def analyze_frame(self, frame) -> tuple[list[PoseDetection], list[tuple]]:
        """Run the pose model once; return ``(detections, person_boxes)``.

        ``detections`` are the looking-away flags (as :meth:`analyze`).
        ``person_boxes`` is ``[(bbox_xyxy, confidence), …]`` for **every** person
        the model detected this frame — independent of head pose — so the
        evidence layer can track people from these YOLO boxes instead of sweeping
        the whole video again with a Haar cascade (H6). Both come from the *same*
        inference, so collecting the boxes costs nothing extra.
        """
        # Pass device explicitly; `0` (GPU) is falsy so it must not be gated.
        predict_kwargs = {'verbose': False, 'device': self.device}

        results = self.model(frame, **predict_kwargs)

        detections: list[PoseDetection] = []
        person_boxes: list[tuple] = []
        for result in results:
            boxes = getattr(result, 'boxes', None)
            # Record every detected person's box for tracking, before any of the
            # looking-away early-exits below can skip a person.
            if boxes is not None:
                for box in boxes:
                    try:
                        person_bbox = tuple(float(v) for v in box.xyxy[0].tolist())
                        person_conf = float(box.conf[0])
                    except (AttributeError, IndexError, TypeError, ValueError):
                        continue
                    person_boxes.append((person_bbox, person_conf))

            keypoints = getattr(result, 'keypoints', None)
            if keypoints is None or keypoints.xy is None:
                continue

            xy = keypoints.xy  # (persons, 17, 2)
            conf = getattr(keypoints, 'conf', None)  # (persons, 17) or None

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
                # Horizontal-only offset → looking DOWN never registers here.
                offset_ratio = abs(nose_x - eye_midpoint_x) / eye_distance
                # Convert the offset into an approximate sideways yaw angle:
                # yaw ≈ atan(offset_ratio / nose_depth_ratio). Flag only when the
                # head is turned past the configured threshold (45° by default).
                yaw_deg = math.degrees(math.atan(offset_ratio / self.nose_depth_ratio))
                if yaw_deg <= self.angle_threshold_deg:
                    continue

                bbox = (0.0, 0.0, 0.0, 0.0)
                if boxes is not None and person_idx < len(boxes):
                    bbox = tuple(
                        float(v) for v in boxes[person_idx].xyxy[0].tolist()
                    )

                # Confidence scales from 0 at the threshold angle to 1.0 at a
                # full 90° profile turn.
                confidence = max(
                    0.0,
                    min(
                        1.0,
                        (yaw_deg - self.angle_threshold_deg)
                        / max(90.0 - self.angle_threshold_deg, 1e-3),
                    ),
                )
                detections.append(
                    PoseDetection(
                        behavior_type=config.LOOKING_AWAY,
                        confidence=confidence,
                        bbox=bbox,
                    )
                )
        return detections, person_boxes

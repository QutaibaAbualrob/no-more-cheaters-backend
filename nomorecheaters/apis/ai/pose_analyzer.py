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

from dataclasses import dataclass

from . import config

# COCO-17 pose keypoint indices used by the heuristic.
_NOSE = 0
_LEFT_EYE = 1
_RIGHT_EYE = 2
_LEFT_EAR = 3
_RIGHT_EAR = 4
_LEFT_SHOULDER = 5
_RIGHT_SHOULDER = 6


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
        *,
        ear_visible_conf: float = config.EAR_VISIBLE_CONF,
        ear_hidden_conf: float = config.EAR_HIDDEN_CONF,
        ear_conf_ratio: float = config.EAR_CONF_RATIO,
        nose_shoulder_sideways: float = config.NOSE_SHOULDER_SIDEWAYS,
        nose_shoulder_turned: float = config.NOSE_SHOULDER_TURNED,
        min_votes: int = config.LOOKING_AWAY_MIN_VOTES,
        imgsz: int = config.POSE_IMGSZ,
        half: bool = config.POSE_HALF,
    ):
        self.model_path = model_path
        self.keypoint_confidence = keypoint_confidence
        # --- legacy params (kept for back-compat; unused by the v2 heuristic) --
        self.angle_threshold_deg = angle_threshold_deg
        self.nose_depth_ratio = max(1e-3, nose_depth_ratio)
        # --- v2 head-turn heuristic thresholds --------------------------------
        self.ear_visible_conf = ear_visible_conf
        self.ear_hidden_conf = ear_hidden_conf
        self.ear_conf_ratio = ear_conf_ratio
        self.nose_shoulder_sideways = nose_shoulder_sideways
        self.nose_shoulder_turned = max(nose_shoulder_sideways + 1e-3, nose_shoulder_turned)
        self.min_votes = max(1, int(min_votes))
        self.imgsz = imgsz
        self.half = half
        # `0` means GPU; `'cpu'` means CPU. Resolved at analysis time.
        self.device = device if device is not None else config.resolve_device()
        self._model = None

    @property
    def model(self):
        """Lazily load the pose weights onto the device (cached + warmed per process).

        Delegates to the shared model cache so a long-lived worker loads the pose
        weights once and reuses them across jobs, with a one-shot warmup on first
        load. Ultralytics downloads the weights here if not already cached.
        """
        if self._model is None:
            from .model_cache import load_model

            self._model = load_model(
                self.model_path, self.device,
                half=self.half, warmup_imgsz=self.imgsz,
            )
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
        predict_kwargs = {'verbose': False, 'device': self.device, 'imgsz': self.imgsz}
        # FP16 helps only on GPU (no benefit on CPU, and some torch ops reject it),
        # so pass half=True only when the device is not CPU. Gate on the resolved
        # device, NOT on `if self.half` alone (device 0 is falsy).
        if self.half and self.device != 'cpu':
            predict_kwargs['half'] = True

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
            if conf is None:
                # Some predict configs expose only the packed (persons, 17, 3)
                # tensor; recover per-keypoint confidence from its last channel.
                data = getattr(keypoints, 'data', None)
                if data is not None and getattr(data, 'shape', (0,))[-1] >= 3:
                    conf = data[..., 2]

            for person_idx in range(len(xy)):
                person_kpts = xy[person_idx]
                if len(person_kpts) <= _RIGHT_SHOULDER:
                    continue

                kc = conf[person_idx] if conf is not None else None
                min_conf = self.keypoint_confidence

                def _conf(i):
                    return float(kc[i]) if kc is not None else 0.0

                def _x(i):
                    return float(person_kpts[i][0])

                # --- V2: horizontal nose-vs-shoulder offset (MANDATORY) -------
                # Pure horizontal yaw signal, normalised by shoulder width so it
                # is distance-invariant. Looking DOWN leaves the nose centred
                # between the shoulders, so V2 stays low and never flags pitch.
                vote_offset = False
                offset_ratio = 0.0
                if (
                    _conf(_NOSE) >= min_conf
                    and _conf(_LEFT_SHOULDER) >= min_conf
                    and _conf(_RIGHT_SHOULDER) >= min_conf
                ):
                    shoulder_width = abs(_x(_LEFT_SHOULDER) - _x(_RIGHT_SHOULDER))
                    if shoulder_width >= 1e-3:
                        shoulder_mid_x = (_x(_LEFT_SHOULDER) + _x(_RIGHT_SHOULDER)) / 2.0
                        offset_ratio = abs(_x(_NOSE) - shoulder_mid_x) / shoulder_width
                        vote_offset = offset_ratio > self.nose_shoulder_sideways

                if not vote_offset:
                    # Without a horizontal turn there is no looking-away — this is
                    # what makes a head bowed over paper (pure pitch) safe.
                    continue

                # --- V1: ear-visibility asymmetry -----------------------------
                c_left_ear, c_right_ear = _conf(_LEFT_EAR), _conf(_RIGHT_EAR)
                hi, lo = max(c_left_ear, c_right_ear), min(c_left_ear, c_right_ear)
                vote_ear = hi >= self.ear_visible_conf and (
                    lo < self.ear_hidden_conf or hi / max(lo, 1e-3) > self.ear_conf_ratio
                )

                # --- V3: a deep offset is self-corroborating ------------------
                vote_deep = offset_ratio >= self.nose_shoulder_turned

                if (int(vote_offset) + int(vote_ear) + int(vote_deep)) < self.min_votes:
                    continue

                bbox = self._person_bbox(boxes, person_idx, person_kpts, kc, min_conf)

                # Confidence ramps 0 -> 1 from the sideways to the turned-back
                # threshold.
                confidence = max(0.0, min(1.0,
                    (offset_ratio - self.nose_shoulder_sideways)
                    / (self.nose_shoulder_turned - self.nose_shoulder_sideways)))

                detections.append(
                    PoseDetection(
                        behavior_type=config.LOOKING_AWAY,
                        confidence=confidence,
                        bbox=bbox,
                    )
                )
        return detections, person_boxes

    @staticmethod
    def _person_bbox(boxes, person_idx, person_kpts, kc, min_conf):
        """Return an xyxy person box: the detector box when available, else the
        bounding extent of the confidently-detected keypoints.

        Falling back to the keypoint extent (instead of ``(0,0,0,0)``) guarantees
        the looking-away event carries a real, drawable box and a distinct centre
        for per-person track separation (H11).
        """
        if boxes is not None and person_idx < len(boxes):
            try:
                return tuple(float(v) for v in boxes[person_idx].xyxy[0].tolist())
            except (IndexError, AttributeError, TypeError):
                pass
        xs, ys = [], []
        for i in range(len(person_kpts)):
            if kc is None or float(kc[i]) >= min_conf:
                x, y = float(person_kpts[i][0]), float(person_kpts[i][1])
                if x > 0 or y > 0:
                    xs.append(x)
                    ys.append(y)
        if len(xs) >= 2:
            return (min(xs), min(ys), max(xs), max(ys))
        return (0.0, 0.0, 0.0, 0.0)

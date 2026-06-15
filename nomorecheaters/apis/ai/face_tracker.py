"""Per-person face tracking and per-alert evidence extraction.

Built entirely on OpenCV (already a pipeline dependency) — no extra packages.
It provides three capabilities used to enrich :class:`~apis.models.Alert` rows
with visual evidence for the analysis report:

1. :func:`track_persons` sweeps the video with a Haar-cascade face detector and
   greedily links face detections across frames into persistent *tracks*. Tracks
   are labelled ``person_1``, ``person_2`` … ordered left-to-right by mean
   horizontal position, so the same person keeps the same label for the whole
   video (matching the live-overlay convention on the frontend).
2. :func:`extract_face_crop` saves the cropped face of the flagged person at an
   alert's timestamp (falls back to a centred crop when no face is tracked).
3. :func:`extract_clip` saves a short clip centred on an alert's timestamp
   (1s before, 2s after by default).

Everything degrades gracefully: any OpenCV/IO failure returns ``None``/``False``
rather than raising, so a missing artifact never fails the whole analysis.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field


# Greedy-matching distance threshold, as a fraction of the frame diagonal. A
# face whose centroid moves less than this between sampled frames is treated as
# the same person.
_MATCH_DIST_FRACTION = 0.18
# A track stays "active" (eligible to absorb new detections) for this many
# seconds after its last sighting; beyond that a reappearance starts a new track.
_TRACK_TIMEOUT_SEC = 3.0


@dataclass
class _Track:
    """Internal mutable accumulator for one person's face across frames."""

    samples: list = field(default_factory=list)  # (timestamp_sec, (x1,y1,x2,y2))
    last_centroid: tuple = (0.0, 0.0)
    last_timestamp: float = -1.0
    sum_cx: float = 0.0
    count: int = 0

    @property
    def mean_cx(self) -> float:
        return self.sum_cx / self.count if self.count else 0.0


@dataclass
class PersonIndex:
    """Query interface over the tracked persons in a video."""

    # person_id -> list of (timestamp_sec, bbox_xyxy), sorted by timestamp.
    tracks: dict
    frame_width: int
    frame_height: int

    @property
    def person_count(self) -> int:
        return len(self.tracks)

    def query(self, timestamp_sec: float, max_gap_sec: float = 2.0):
        """Return ``(person_id, bbox_xyxy)`` for the face nearest *timestamp_sec*.

        Picks the track with a sighting closest in time to the alert (within
        *max_gap_sec*); ties break toward the largest face (closest to camera).
        Returns ``("person_1", None)`` when nobody was tracked.
        """
        best = None  # (time_gap, -area, person_id, bbox)
        for person_id, samples in self.tracks.items():
            for ts, bbox in samples:
                gap = abs(ts - timestamp_sec)
                if gap > max_gap_sec:
                    continue
                area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                candidate = (gap, -area, person_id, bbox)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
        if best is None:
            # Nobody tracked near this moment: attribute to the first/only person.
            if self.tracks:
                first = next(iter(self.tracks))
                return first, None
            return 'person_1', None
        return best[2], best[3]


def _cascade():
    """Load the bundled frontal-face Haar cascade, or ``None`` if unavailable."""
    import cv2

    path = os.path.join(cv2.data.haarcascades, 'haarcascade_frontalface_default.xml')
    classifier = cv2.CascadeClassifier(path)
    return None if classifier.empty() else classifier


def track_persons(video_path: str, sample_every_n: int = 15) -> PersonIndex:
    """Sweep *video_path* and link face detections into per-person tracks.

    Faces are detected on every *sample_every_n*-th frame and matched to the
    nearest recent track by centroid distance. Tracks are then relabelled
    ``person_1..N`` left-to-right by mean horizontal position.
    """
    import cv2

    empty = PersonIndex(tracks={}, frame_width=0, frame_height=0)
    classifier = _cascade()
    if classifier is None:
        return empty

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        return empty

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        fps = 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    diagonal = math.hypot(width, height) or 1.0
    match_threshold = diagonal * _MATCH_DIST_FRACTION

    tracks: list[_Track] = []
    sample_every_n = max(1, int(sample_every_n))

    try:
        frame_number = 0
        while True:
            grabbed, frame = capture.read()
            if not grabbed:
                break
            if frame_number % sample_every_n == 0:
                timestamp = frame_number / fps
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
                )
                _assign_faces(tracks, faces, timestamp, match_threshold)
            frame_number += 1
    finally:
        capture.release()

    # Relabel left-to-right so labels are stable and intuitive in the report.
    tracks.sort(key=lambda t: t.mean_cx)
    labelled = {
        f'person_{i + 1}': sorted(track.samples, key=lambda s: s[0])
        for i, track in enumerate(tracks)
    }
    return PersonIndex(tracks=labelled, frame_width=width, frame_height=height)


def _assign_faces(tracks: list[_Track], faces, timestamp: float, match_threshold: float) -> None:
    """Greedily match this frame's detected faces to existing tracks."""
    for (x, y, w, h) in faces:
        cx, cy = x + w / 2.0, y + h / 2.0
        bbox = (float(x), float(y), float(x + w), float(y + h))

        best_track = None
        best_dist = match_threshold
        for track in tracks:
            if timestamp - track.last_timestamp > _TRACK_TIMEOUT_SEC:
                continue
            dist = math.hypot(cx - track.last_centroid[0], cy - track.last_centroid[1])
            if dist < best_dist:
                best_dist = dist
                best_track = track

        if best_track is None:
            best_track = _Track()
            tracks.append(best_track)

        best_track.samples.append((timestamp, bbox))
        best_track.last_centroid = (cx, cy)
        best_track.last_timestamp = timestamp
        best_track.sum_cx += cx
        best_track.count += 1


def _read_frame_at(capture, timestamp_sec: float, fps: float):
    """Seek a capture to *timestamp_sec* and return that frame (or ``None``)."""
    import cv2

    target = max(0, int(round(timestamp_sec * fps)))
    capture.set(cv2.CAP_PROP_POS_FRAMES, target)
    grabbed, frame = capture.read()
    if grabbed:
        return frame
    # Fallback: rewind and read the first frame.
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    grabbed, frame = capture.read()
    return frame if grabbed else None


def extract_face_crop(video_path: str, timestamp_sec: float, bbox, out_path: str,
                      pad: float = 0.3) -> bool:
    """Save a cropped face JPEG at *timestamp_sec* to *out_path*.

    *bbox* is an ``(x1, y1, x2, y2)`` face box (typically from :func:`track_persons`);
    when ``None`` a centred crop of the frame is used as a best-effort fallback.
    Returns ``True`` on success.
    """
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        return False
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        fps = 30.0
    try:
        frame = _read_frame_at(capture, timestamp_sec, fps)
        if frame is None:
            return False
        h, w = frame.shape[:2]
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            bw, bh = (x2 - x1), (y2 - y1)
            x1 = int(max(0, x1 - bw * pad))
            y1 = int(max(0, y1 - bh * pad))
            x2 = int(min(w, x2 + bw * pad))
            y2 = int(min(h, y2 + bh * pad))
        else:
            # Centred head-region fallback when no face was tracked.
            cw, ch = int(w * 0.34), int(h * 0.46)
            x1 = (w - cw) // 2
            y1 = int(h * 0.16)
            x2, y2 = x1 + cw, y1 + ch
        if x2 <= x1 or y2 <= y1:
            return False
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        return bool(cv2.imwrite(out_path, crop))
    finally:
        capture.release()


def _make_writer(cv2, out_path: str, fps: float, size):
    """Create a VideoWriter, preferring H.264 (avc1) for browser playback.

    Falls back to mp4v if the H.264 encoder is unavailable in this OpenCV build.
    """
    for fourcc_name in ('avc1', 'mp4v'):
        fourcc = cv2.VideoWriter_fourcc(*fourcc_name)
        writer = cv2.VideoWriter(str(out_path), fourcc, fps, size)
        if writer.isOpened():
            return writer
        writer.release()
    return None


def extract_clip(video_path: str, timestamp_sec: float, out_path: str,
                 before: float = 1.0, after: float = 2.0) -> bool:
    """Save a clip spanning ``[t-before, t+after]`` to *out_path* (mp4).

    Returns ``True`` when a non-empty clip was written.
    """
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        return False
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 0:
        fps = 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width <= 0 or height <= 0:
        capture.release()
        return False

    start_frame = max(0, int((timestamp_sec - before) * fps))
    end_frame = int((timestamp_sec + after) * fps)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    writer = _make_writer(cv2, out_path, fps, (width, height))
    if writer is None:
        capture.release()
        return False

    written = 0
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        current = start_frame
        while current <= end_frame:
            grabbed, frame = capture.read()
            if not grabbed:
                break
            writer.write(frame)
            written += 1
            current += 1
    finally:
        writer.release()
        capture.release()
    return written > 0

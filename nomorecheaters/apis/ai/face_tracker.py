"""Per-person face tracking and per-alert evidence extraction.

Built entirely on OpenCV (already a pipeline dependency) — no extra packages.
It provides capabilities used to enrich :class:`~apis.models.Alert` rows
with visual evidence for the analysis report:

1. :func:`index_from_person_boxes` builds the per-person tracks from boxes the
   YOLO pose pass already produced (see :func:`apis.ai.detector.analyze_video`),
   so the main pipeline never scans the video an extra time (H6).
   :func:`track_persons` is the standalone fallback: it sweeps the video with a
   Haar-cascade face detector when no YOLO boxes are available (e.g. a direct
   evidence call outside the pipeline). Both greedily link detections across
   frames into persistent *tracks* labelled ``person_1``, ``person_2`` … ordered
   left-to-right by mean horizontal position, so the same person keeps the same
   label for the whole video (matching the live-overlay convention on the
   frontend).
2. :func:`extract_face_crop` saves the cropped face of the flagged person at an
   alert's timestamp (falls back to a centred crop when no face is tracked).
3. :func:`extract_clip` saves a short clip that STARTS at an alert's timestamp
   (0s before, 3s after by default) so the clip shows the cheating itself, not
   the lead-up to it.

Everything degrades gracefully: any OpenCV/IO failure returns ``None``/``False``
rather than raising, so a missing artifact never fails the whole analysis.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
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
    # Whether the stored boxes are whole-person boxes (from YOLO, H6) or face
    # boxes (from the Haar fallback). The evidence layer crops a head region out
    # of a person box for the avatar, but uses a face box as-is.
    box_kind: str = 'face'

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

    def persons_at(self, timestamp_sec: float, max_gap_sec: float = 2.0):
        """Return ``{person_id: bbox_xyxy}`` for every person sighted near *timestamp_sec*.

        For each tracked person the sighting closest in time (within
        *max_gap_sec*) is used. Lets the evidence drawer box *all* people in a
        frame — the flagged one in green, the rest in gray.
        """
        result = {}
        for person_id, samples in self.tracks.items():
            best = None  # (gap, bbox)
            for ts, bbox in samples:
                gap = abs(ts - timestamp_sec)
                if gap > max_gap_sec:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, bbox)
            if best is not None:
                result[person_id] = best[1]
        return result

    def person_for_box(self, timestamp_sec: float, object_bbox, max_gap_sec: float = 2.0):
        """Return ``(person_id, person_bbox)`` for the person nearest *object_bbox*.

        Attributes a detected object (a phone/laptop box) to the person actually
        holding/next to it, instead of the largest person in frame. Among the
        people sighted within *max_gap_sec* of *timestamp_sec*, it prefers the one
        whose box **contains the object's centre**; ties and non-containment fall
        back to the greatest box overlap (IoU), then to the nearest centre. Returns
        ``(None, None)`` when there is no usable box or nobody was sighted near the
        time, so the caller falls back to its time-based pick (:meth:`query`).
        """
        if not object_bbox:
            return None, None
        ox1, oy1, ox2, oy2 = object_bbox
        ocx, ocy = (ox1 + ox2) / 2.0, (oy1 + oy2) / 2.0

        best = None  # (contains_centre, iou, -centre_distance)
        for person_id, bbox in self.persons_at(timestamp_sec, max_gap_sec).items():
            bx1, by1, bx2, by2 = bbox
            contains = 1 if (bx1 <= ocx <= bx2 and by1 <= ocy <= by2) else 0
            iou = _iou(object_bbox, bbox)
            bcx, bcy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
            dist = ((ocx - bcx) ** 2 + (ocy - bcy) ** 2) ** 0.5
            score = (contains, iou, -dist)
            if best is None or score > best[0]:
                best = (score, person_id, bbox)
        if best is None:
            return None, None
        return best[1], best[2]

    def to_overlay_dict(self, max_points_per_person: int = 3000) -> dict:
        """Serialize the tracks for the frontend's client-side box overlay.

        Returns ``{frame_width, frame_height, persons: {pid: [[t, x1, y1, x2, y2],
        …]}}`` — integer pixel coords in source-frame space (the same space the
        original video plays in, so the browser scales them with a simple
        letterbox transform) and times in seconds rounded to milliseconds.

        Long tracks are stride-downsampled to *max_points_per_person* so the JSON
        the report ships stays small; the overlay interpolates between the
        surviving samples, so on-screen motion stays smooth. The first and last
        sample of every track are always kept so a box never appears late or
        vanishes early.
        """
        def _point(sample):
            ts, bbox = sample
            x1, y1, x2, y2 = bbox
            return [round(float(ts), 3), int(round(x1)), int(round(y1)),
                    int(round(x2)), int(round(y2))]

        persons: dict = {}
        for person_id, samples in self.tracks.items():
            if not samples:
                continue
            step = 1
            if max_points_per_person and len(samples) > max_points_per_person:
                step = (len(samples) // max_points_per_person) + 1
            points = [_point(samples[i]) for i in range(0, len(samples), step)]
            if (len(samples) - 1) % step != 0:
                points.append(_point(samples[-1]))  # keep the true track end
            persons[person_id] = points

        return {
            'frame_width': self.frame_width,
            'frame_height': self.frame_height,
            'persons': persons,
        }


def _iou(box_a, box_b) -> float:
    """Intersection-over-union of two ``(x1, y1, x2, y2)`` boxes (0.0 if disjoint)."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


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

    return _index_from_tracks(tracks, width, height, box_kind='face')


def index_from_person_boxes(frame_boxes, frame_width: int, frame_height: int) -> PersonIndex:
    """Build a :class:`PersonIndex` from pre-detected per-frame person boxes.

    *frame_boxes* is an iterable of ``(timestamp_sec, [bbox_xyxy, …])`` in
    ascending time order — exactly what the YOLO pose pass harvests during the
    main analysis (see :func:`apis.ai.detector.analyze_video`). The same greedy
    centroid tracker and left-to-right labelling as :func:`track_persons` are
    reused, but no video is opened: this consumes boxes the pipeline already has,
    eliminating the redundant Haar-cascade sweep (H6). Requires no OpenCV, so it
    also runs in tests without model weights or a real video.
    """
    diagonal = math.hypot(frame_width, frame_height) or 1.0
    match_threshold = diagonal * _MATCH_DIST_FRACTION

    tracks: list[_Track] = []
    for timestamp, boxes in frame_boxes:
        # Reuse the (x, y, w, h)-based matcher: convert each xyxy box first.
        faces = [
            (x1, y1, x2 - x1, y2 - y1)
            for (x1, y1, x2, y2) in boxes
            if x2 > x1 and y2 > y1
        ]
        _assign_faces(tracks, faces, float(timestamp), match_threshold)

    return _index_from_tracks(tracks, frame_width, frame_height, box_kind='person')


def _index_from_tracks(tracks: list[_Track], width: int, height: int,
                       box_kind: str) -> PersonIndex:
    """Relabel raw tracks left-to-right into a queryable :class:`PersonIndex`."""
    tracks.sort(key=lambda t: t.mean_cx)
    labelled = {
        f'person_{i + 1}': sorted(track.samples, key=lambda s: s[0])
        for i, track in enumerate(tracks)
    }
    return PersonIndex(
        tracks=labelled, frame_width=width, frame_height=height, box_kind=box_kind,
    )


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


# How many seconds of video we are willing to decode forward to correct a
# keyframe-snapped seek. Bounds the forward-decode so a backend whose reported
# position never advances cannot spin; 12s comfortably exceeds any real keyframe
# (GOP) interval, so a genuine snap is always reached within the budget.
_MAX_SEEK_CORRECTION_SEC = 12.0


def _seek_to_frame(capture, target_frame: int, fps: float) -> None:
    """Position *capture* so the next ``read()`` returns ``target_frame``.

    ``cap.set(CAP_PROP_POS_FRAMES, n)`` lands on the nearest *preceding* keyframe
    on many backends (notably OpenCV's FFmpeg backend on Windows), so the next
    read can be several seconds *before* the requested frame — the root cause of
    evidence clips/snapshots starting before the detected moment. When the
    backend reports where it actually landed and that is *before* the target, we
    decode forward (``grab``) to the real frame.

    Defensive by construction: if the backend reports ``0``/garbage or claims it
    is already at/after the target — i.e. we cannot trust the position — no frames
    are skipped and we fall back to the backend's own seek, so this is never worse
    than the bare ``set``. The forward-decode is bounded by
    :data:`_MAX_SEEK_CORRECTION_SEC` so it can never spin.
    """
    import cv2

    capture.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    try:
        landed = int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0)
    except Exception:  # noqa: BLE001 — unreadable position → trust the raw seek
        return
    # Only correct a genuine backward snap. landed == 0 (common "don't know"
    # reading) or landed >= target (already accurate, or a lying backend) is left
    # untouched so we never skip a whole clip's worth of frames on a backend that
    # cannot report position.
    if not (0 < landed < target_frame):
        return
    budget = int(max(1.0, fps) * _MAX_SEEK_CORRECTION_SEC)
    for _ in range(min(target_frame - landed, budget)):
        if not capture.grab():
            break


def _boxes_from_index(person_index, timestamp_sec: float):
    """Per-frame ``[(person_id, bbox), …]`` from a :class:`PersonIndex`, or ``None``.

    Used by :func:`extract_clip` to move the overlay with the people across the
    clip. Fully defensive: any error (or an index that is not a real
    ``PersonIndex``) yields ``None`` so the caller falls back to its static box.
    """
    try:
        live = person_index.persons_at(timestamp_sec)
    except Exception:  # noqa: BLE001 — best-effort: fall back to the static box
        return None
    if not live:
        return None
    return list(live.items())


def _read_frame_at(capture, timestamp_sec: float, fps: float):
    """Seek a capture to *timestamp_sec* and return that frame (or ``None``)."""
    import cv2

    target = max(0, int(round(timestamp_sec * fps)))
    _seek_to_frame(capture, target, fps)
    grabbed, frame = capture.read()
    if grabbed:
        return frame
    # Fallback: rewind and read the first frame.
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    grabbed, frame = capture.read()
    return frame if grabbed else None


def _fallback_bbox(width: int, height: int):
    """A centred head-region box used when no face was tracked at an alert."""
    cw, ch = int(width * 0.34), int(height * 0.46)
    x1 = (width - cw) // 2
    y1 = int(height * 0.16)
    return (x1, y1, x1 + cw, y1 + ch)


def person_label(person_id) -> str:
    """``'person_1' -> 'Person 1'`` (falls back to a title-cased id)."""
    text = str(person_id or 'person_1')
    suffix = text.rsplit('_', 1)[-1]
    if suffix.isdigit():
        return f'Person {int(suffix)}'
    return text.replace('_', ' ').title()


# BGR colours for the evidence overlay.
_FLAGGED_COLOR = (0, 255, 0)      # green — the person who triggered the alert
_OTHER_COLOR = (128, 128, 128)    # gray — everyone else in frame


def _draw_box_with_label(cv2, frame, bbox, color, label: str) -> None:
    """Draw a rectangle (2px) plus a label above it, clamped to the frame.

    The label sits on a filled dark strip so coloured text stays legible over
    any background. Drops below the top edge when there is no room above.
    """
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (int(round(v)) for v in bbox)
    x1 = max(0, min(x1, w - 1))
    x2 = max(0, min(x2, w - 1))
    y1 = max(0, min(y1, h - 1))
    y2 = max(0, min(y2, h - 1))
    if x2 <= x1 or y2 <= y1:
        return

    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    if not label:
        return

    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.6
    thickness = 2
    (tw, th), baseline = cv2.getTextSize(label, font, scale, thickness)
    ty = y1 - 8
    if ty - th - baseline < 0:        # no room above → place just inside the box
        ty = min(h - baseline - 1, y1 + th + 8)
    tx = max(0, min(x1, w - tw - 1))
    cv2.rectangle(
        frame,
        (tx, ty - th - baseline),
        (min(w - 1, tx + tw), min(h - 1, ty + baseline)),
        (0, 0, 0),
        -1,
    )
    cv2.putText(frame, label, (tx, ty), font, scale, color, thickness, cv2.LINE_AA)


def _draw_person_boxes(cv2, frame, boxes, flagged_person_id, behavior_label: str) -> None:
    """Annotate *frame* in place: green box+behaviour on the flagged person,
    gray boxes with just the person id on everyone else.

    *boxes* is an iterable of ``(person_id, bbox_xyxy)``.
    """
    for person_id, bbox in boxes:
        if bbox is None:
            continue
        if person_id == flagged_person_id:
            label = person_label(person_id)
            if behavior_label:
                label = f'{label} - {behavior_label}'
            _draw_box_with_label(cv2, frame, bbox, _FLAGGED_COLOR, label)
        else:
            _draw_box_with_label(cv2, frame, bbox, _OTHER_COLOR, person_label(person_id))


def extract_face_crop(video_path: str, timestamp_sec: float, bbox, out_path: str,
                      pad: float = 0.3, head_fraction: float | None = None) -> bool:
    """Save a cropped face JPEG at *timestamp_sec* to *out_path* (no overlay).

    *bbox* is an ``(x1, y1, x2, y2)`` box; when ``None`` a centred crop of the
    frame is used as a best-effort fallback. The crop is taken from the
    unmodified frame so the avatar shows a clean face.

    *head_fraction* handles whole-person boxes (from YOLO, H6): when set, the box
    is first narrowed to its top fraction of height — the head/shoulders region —
    so a tall full-body box still yields a face-like avatar rather than a tiny
    standing figure. Leave it ``None`` for face boxes (the Haar fallback), which
    are cropped as-is. Returns ``True`` on success.
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
            if head_fraction is not None:
                # Person box → keep only the top slice (head/shoulders) so the
                # avatar isn't a tiny full-body figure.
                y2 = y1 + (y2 - y1) * max(0.05, min(1.0, head_fraction))
            bw, bh = (x2 - x1), (y2 - y1)
            x1 = int(max(0, x1 - bw * pad))
            y1 = int(max(0, y1 - bh * pad))
            x2 = int(min(w, x2 + bw * pad))
            y2 = int(min(h, y2 + bh * pad))
        else:
            x1, y1, x2, y2 = _fallback_bbox(w, h)
        if x2 <= x1 or y2 <= y1:
            return False
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        return bool(cv2.imwrite(out_path, crop))
    finally:
        capture.release()


def extract_annotated_frame(video_path: str, timestamp_sec: float, boxes,
                            flagged_person_id, behavior_label: str, out_path: str) -> bool:
    """Save the FULL frame at *timestamp_sec* with person boxes drawn, to *out_path*.

    The flagged person gets a green box labelled ``"Person N — <Behaviour>"``;
    any other tracked people get gray boxes labelled ``"Person N"``. When no
    boxes are available a centred green fallback box is drawn so the report still
    shows where to look. Drawing happens on a copy so the source frame is never
    mutated. Returns ``True`` on success.
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
        annotated = frame.copy()
        drawable = [(pid, bb) for pid, bb in (boxes or []) if bb is not None]
        if drawable:
            _draw_person_boxes(cv2, annotated, drawable, flagged_person_id, behavior_label)
        else:
            h, w = annotated.shape[:2]
            label = person_label(flagged_person_id)
            if behavior_label:
                label = f'{label} - {behavior_label}'
            _draw_box_with_label(cv2, annotated, _fallback_bbox(w, h), _FLAGGED_COLOR, label)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        return bool(cv2.imwrite(out_path, annotated))
    finally:
        capture.release()


def _make_writer(cv2, out_path: str, fps: float, size):
    """Create a VideoWriter for the temporary clip.

    ``mp4v`` is tried FIRST because it is always present in OpenCV's bundled
    FFmpeg and never touches the (often broken on Windows) OpenH264 library that
    ``avc1`` needs — attempting ``avc1`` first spams "Incorrect library version"
    errors and fails to initialise. The clip written here is only an
    intermediate: :func:`extract_clip` re-encodes it to real H.264 with ffmpeg
    afterwards, so the temp codec just has to be reliable, not web-playable.
    """
    for fourcc_name in ('mp4v', 'avc1'):
        fourcc = cv2.VideoWriter_fourcc(*fourcc_name)
        writer = cv2.VideoWriter(str(out_path), fourcc, fps, size)
        if writer.isOpened():
            return writer
        writer.release()
    return None


def _ffmpeg_exe():
    """Locate an ffmpeg binary: system ``PATH`` first, then bundled imageio-ffmpeg.

    Returns the executable path, or ``None`` when neither is available. The
    ``imageio-ffmpeg`` pip package ships a static ffmpeg, so the H.264 re-encode
    works even on machines with no system ffmpeg installed.
    """
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — package missing/broken → no re-encode
        return None


def _reencode_h264(src_path: str, dst_path: str, timeout: float = 120) -> bool:
    """Re-encode *src_path* to a browser-playable H.264 MP4 at *dst_path*.

    OpenCV writes the temp clip as ``mp4v`` (MPEG-4 Part 2), which Chrome/Firefox
    play poorly or not at all (they show one frame then freeze). We transcode to
    ``libx264`` + ``yuv420p`` (the pixel format browsers require) with
    ``+faststart`` so the clip streams inline. ffmpeg comes from the system or
    the bundled :func:`_ffmpeg_exe`. Returns ``True`` only when a non-empty output
    file was produced; the caller keeps the mp4v clip otherwise, so a missing
    ffmpeg never breaks analysis.

    *timeout* bounds the ffmpeg call so a wedged process can never hang the rq
    worker forever (M7). The default suits the short evidence clips; the
    full-length annotated-video re-encode passes a larger, duration-scaled
    budget (see :func:`apis.ai.detector._reencode_timeout_for`) so a long but
    healthy transcode is not killed prematurely. A timeout — like any ffmpeg
    failure — returns ``False``, leaving the caller to fall back to the mp4v file.
    """
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [
                ffmpeg, '-y',
                '-i', str(src_path),
                '-vcodec', 'libx264',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-an',  # the source clip carries no audio track
                str(dst_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0


def extract_clip(video_path: str, timestamp_sec: float, out_path: str,
                 before: float = 0.0, after: float = 3.0,
                 boxes=None, flagged_person_id=None, behavior_label: str = '',
                 person_index=None) -> bool:
    """Save a clip spanning ``[t-before, t+after]`` to *out_path* (mp4).

    Defaults capture ``[t, t+3s]`` — the clip starts exactly AT the detected
    cheating moment and shows the 3 seconds of evidence after it, rather than
    the second before it. The end is naturally clamped to the video duration
    (the read loop stops when frames run out) and the start is clamped to 0. The
    seek itself is keyframe-corrected (see :func:`_seek_to_frame`) so the clip
    actually begins at *timestamp_sec* rather than at the preceding keyframe,
    which on some backends sits several seconds earlier.

    When *boxes* (an iterable of ``(person_id, bbox_xyxy)``) is supplied, the
    same overlay drawn on the snapshot is baked onto EVERY clip frame — a green
    box+behaviour label on the flagged person and gray boxes on others — so the
    instructor watching the clip sees exactly who was flagged.

    When *person_index* (a :class:`PersonIndex`) is also supplied, the overlay
    *follows* the people across the clip: each frame uses the boxes tracked
    nearest that moment, falling back to the static *boxes* whenever the index has
    no sighting of the flagged person near that frame, so the green box never
    blinks out. Omit it to keep the boxes static (re-detecting per frame is too
    slow); a centred fallback box is used when nobody was tracked at all.

    The frames are first written with OpenCV; the result is then re-encoded to
    browser-friendly H.264 with ffmpeg when that binary is installed (see
    :func:`_reencode_h264`). Returns ``True`` when a non-empty clip exists at
    *out_path*.
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

    # Static overlay reused on every clip frame.
    drawable = [(pid, bb) for pid, bb in (boxes or []) if bb is not None]
    fallback = None
    if not drawable and (boxes is not None or flagged_person_id is not None):
        label = person_label(flagged_person_id)
        if behavior_label:
            label = f'{label} — {behavior_label}'
        fallback = (_fallback_bbox(width, height), label)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # OpenCV writes to a temp file; ffmpeg then transcodes it to the final
    # H.264 path. If ffmpeg is missing we just promote the temp file as-is.
    raw_path = f'{out_path}.raw.mp4'
    writer = _make_writer(cv2, raw_path, fps, (width, height))
    if writer is None:
        capture.release()
        _safe_remove(raw_path)  # _make_writer may have left a 0-byte stub
        return False

    # Wrap the intermediate lifecycle so the ``.raw.mp4`` is never orphaned (M8):
    # on success it is moved onto the final path; on a crash between writing and
    # re-encoding the finally still removes it (``_safe_remove`` no-ops if gone).
    try:
        written = 0
        try:
            _seek_to_frame(capture, start_frame, fps)
            current = start_frame
            while current <= end_frame:
                grabbed, frame = capture.read()
                if not grabbed:
                    break
                # Move the overlay with the people when an index is available;
                # otherwise (or when the flagged person isn't sighted near this
                # frame) keep the static box so the green box never disappears.
                frame_boxes = drawable
                if person_index is not None:
                    live = _boxes_from_index(person_index, current / fps)
                    if live is not None and any(pid == flagged_person_id for pid, _ in live):
                        frame_boxes = live
                if frame_boxes:
                    _draw_person_boxes(cv2, frame, frame_boxes, flagged_person_id, behavior_label)
                elif fallback is not None:
                    _draw_box_with_label(cv2, frame, fallback[0], _FLAGGED_COLOR, fallback[1])
                writer.write(frame)
                written += 1
                current += 1
        finally:
            writer.release()
            capture.release()

        if written <= 0:
            return False

        if not _reencode_h264(raw_path, out_path):
            # No ffmpeg (or it failed): keep the OpenCV clip at the final path.
            os.replace(raw_path, out_path)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    finally:
        _safe_remove(raw_path)


def _safe_remove(path: str) -> None:
    """Delete *path* if it exists, ignoring any IO error."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

"""Main orchestrator for the video-analysis pipeline.

Ties the components together:

1. :mod:`frame_extractor` opens the video and samples frames.
2. For each sampled frame, :class:`~apis.ai.yolo_detector.ObjectDetector` and
   :class:`~apis.ai.pose_analyzer.PoseAnalyzer` produce raw frame-level
   detections.
3. A **temporal rules layer** merges same-class detections inside a sliding
   window into consolidated events, keeps the strongest confidence, and drops
   anything too brief to be real.
4. An **annotated MP4** is written with boxes/labels on the frames where
   surviving events occur, preserving the original duration.

The public entry point is :func:`analyze_video`, which returns an
:class:`AnalysisResult`. It deliberately knows nothing about Django models so
it can be tested in isolation; the service/task layer (Task 1.4) maps the
returned events onto :class:`~apis.models.Alert` / ``Report`` rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import config
from .frame_extractor import iter_sampled_frames, read_metadata
from .pose_analyzer import PoseAnalyzer
from .yolo_detector import ObjectDetector


@dataclass
class FrameDetection:
    """A raw, single-frame detection prior to temporal consolidation."""

    behavior_type: str
    confidence: float
    bbox: tuple
    frame_number: int
    timestamp_sec: float


@dataclass
class AlertEvent:
    """A consolidated detection spanning a contiguous slice of the video."""

    behavior_type: str
    confidence: float          # highest confidence across the merged frames
    start_sec: float
    end_sec: float
    frame_count: int           # number of sampled frames that contributed

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec

    @property
    def timestamp_sec(self) -> int:
        """Integer start time, matching ``Alert.timestamp_sec``."""
        return int(self.start_sec)


@dataclass
class AnalysisResult:
    """Everything the caller needs to persist a completed analysis."""

    events: list[AlertEvent]
    annotated_video_path: str | None
    metadata: dict = field(default_factory=dict)


def _collect_frame_detections(
    video_path: str,
    object_detector: ObjectDetector,
    pose_analyzer: PoseAnalyzer,
    sample_every_n: int,
) -> tuple[list[FrameDetection], int]:
    """Run both detectors over every sampled frame.

    The two detectors are independent per frame; they are invoked back to back
    here. (They could be fanned out to threads, but sharing a single CUDA model
    across threads is fragile, so the pipeline keeps them sequential.)
    """
    detections: list[FrameDetection] = []
    frames_analyzed = 0
    for sample in iter_sampled_frames(video_path, sample_every_n=sample_every_n):
        frames_analyzed += 1
        for obj in object_detector.detect(sample.frame):
            detections.append(
                FrameDetection(
                    behavior_type=obj.behavior_type,
                    confidence=obj.confidence,
                    bbox=obj.bbox,
                    frame_number=sample.frame_number,
                    timestamp_sec=sample.timestamp_sec,
                )
            )
        for pose in pose_analyzer.analyze(sample.frame):
            detections.append(
                FrameDetection(
                    behavior_type=pose.behavior_type,
                    confidence=pose.confidence,
                    bbox=pose.bbox,
                    frame_number=sample.frame_number,
                    timestamp_sec=sample.timestamp_sec,
                )
            )
    return detections, frames_analyzed


def _bbox_center(bbox) -> tuple[float, float, float] | None:
    """Return ``(cx, cy, size)`` for a usable bbox, else ``None``.

    ``size`` is the larger box edge, used to scale the "same person" distance
    threshold. A zero/degenerate box (some pose detections report ``(0,0,0,0)``)
    yields ``None`` so it never forces a track split.
    """
    if not bbox:
        return None
    x1, y1, x2, y2 = bbox
    width = x2 - x1
    height = y2 - y1
    if width <= 0 or height <= 0:
        return None
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0, max(width, height))


def _same_track(center_a, center_b, factor: float = 0.75) -> bool:
    """Whether two bbox centres are close enough to be the same person/object.

    The distance threshold scales with box size so it adapts to camera distance.
    Either centre being ``None`` (no usable box) returns ``True`` — such
    detections fall back to pure temporal merging rather than spawning spurious
    extra events.
    """
    if center_a is None or center_b is None:
        return True
    ax, ay, a_size = center_a
    bx, by, b_size = center_b
    distance = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    return distance <= factor * max(a_size, b_size)


def _infer_sample_step(detections: list[FrameDetection]) -> int:
    """Infer the frame-sampling stride from the smallest positive frame gap.

    Frames are sampled every Nth frame, so adjacent samples differ by N. The
    smallest positive difference between any two detection frame numbers is that
    stride; falls back to 1 when it cannot be determined.
    """
    frames = sorted({d.frame_number for d in detections})
    step = min((b - a for a, b in zip(frames, frames[1:])), default=0)
    return step if step > 0 else 1


def _longest_consecutive_run(frame_numbers: list[int], step: int) -> int:
    """Length of the longest run of consecutive sampled frames (gap == *step*)."""
    frames = sorted(set(frame_numbers))
    if not frames:
        return 0
    best = run = 1
    for prev, current in zip(frames, frames[1:]):
        run = run + 1 if (current - prev) == step else 1
        best = max(best, run)
    return best


def consolidate_events(
    detections: list[FrameDetection],
    merge_window_sec: float = config.MERGE_WINDOW_SEC,
    min_duration_sec: float = config.MIN_EVENT_DURATION_SEC,
) -> list[AlertEvent]:
    """Merge frame detections into events via the temporal rules layer.

    Detections of the same class are first separated into **per-person tracks**
    by spatial proximity of their bounding boxes, so two students looking away
    (or two phones in different hands) become two events rather than collapsing
    into one. Within a track, detections whose gap is within *merge_window_sec*
    merge into a single event (max confidence across its frames); a gap larger
    than the window starts a fresh event for that person.

    Surviving events must clear two noise gates:

    * **Duration** — soft pose cues (looking away) must last at least
      *min_duration_sec*. Direct object evidence (phone/laptop) is always kept,
      even a single sighting, since a momentary glimpse is a genuine violation.
    * **Consecutive frames** — a ``LOOKING_AWAY`` event must additionally span at
      least :data:`config.LOOKING_AWAY_MIN_CONSECUTIVE` *consecutive* sampled
      frames, so a single momentary glance (or scattered jitter) never alerts.

    Returns events sorted by start time.
    """
    sample_step = _infer_sample_step(detections)

    by_type: dict[str, list[FrameDetection]] = {}
    for det in detections:
        by_type.setdefault(det.behavior_type, []).append(det)

    # (event, [frame_numbers]) pairs, so the consecutive-frame gate can inspect
    # exactly which sampled frames backed each consolidated event.
    built: list[tuple[AlertEvent, list[int]]] = []

    for behavior_type, items in by_type.items():
        items.sort(key=lambda d: d.timestamp_sec)
        # Each open track: {'event', 'frames', 'last_ts', 'center'}. A detection
        # extends an existing track only when it is within the merge window AND
        # spatially on the same person; otherwise it opens a new event (new
        # person or a resumed behaviour after a long gap).
        tracks: list[dict] = []
        for det in items:
            center = _bbox_center(det.bbox)
            match = None
            for track in tracks:
                if (det.timestamp_sec - track['last_ts']) > merge_window_sec:
                    continue
                if _same_track(center, track['center']):
                    match = track
                    break

            if match is None:
                event = AlertEvent(
                    behavior_type=behavior_type,
                    confidence=det.confidence,
                    start_sec=det.timestamp_sec,
                    end_sec=det.timestamp_sec,
                    frame_count=1,
                )
                frames = [det.frame_number]
                built.append((event, frames))
                tracks.append({
                    'event': event, 'frames': frames,
                    'last_ts': det.timestamp_sec, 'center': center,
                })
            else:
                event = match['event']
                event.end_sec = det.timestamp_sec
                event.confidence = max(event.confidence, det.confidence)
                event.frame_count += 1
                match['frames'].append(det.frame_number)
                match['last_ts'] = det.timestamp_sec
                if center is not None:
                    match['center'] = center  # follow slow movement across frames

    survivors: list[AlertEvent] = []
    for event, frames in built:
        if event.behavior_type in config.DIRECT_EVIDENCE_BEHAVIORS:
            survivors.append(event)  # objects bypass both noise gates
            continue
        if event.duration_sec < min_duration_sec:
            continue
        if event.behavior_type == config.LOOKING_AWAY:
            if _longest_consecutive_run(frames, sample_step) < config.LOOKING_AWAY_MIN_CONSECUTIVE:
                continue
        survivors.append(event)

    survivors.sort(key=lambda e: e.start_sec)
    return survivors


def _surviving_detections_by_frame(
    detections: list[FrameDetection],
    events: list[AlertEvent],
) -> dict[int, list[FrameDetection]]:
    """Index frame detections that fall inside a surviving event window.

    Used by the annotator so only detections backing a real event get drawn.
    """
    windows: dict[str, list[tuple[float, float]]] = {}
    for event in events:
        windows.setdefault(event.behavior_type, []).append(
            (event.start_sec, event.end_sec)
        )

    by_frame: dict[int, list[FrameDetection]] = {}
    for det in detections:
        spans = windows.get(det.behavior_type, ())
        if any(start <= det.timestamp_sec <= end for start, end in spans):
            by_frame.setdefault(det.frame_number, []).append(det)
    return by_frame


def _default_output_path(video_path: str) -> str:
    from pathlib import Path

    path = Path(video_path)
    return str(path.with_name(f'{path.stem}_annotated.mp4'))


def _write_annotated_video(
    video_path: str,
    detections: list[FrameDetection],
    events: list[AlertEvent],
    output_path: str,
) -> str | None:
    """Render an annotated copy of the video.

    Boxes/labels are drawn on the frames whose detections back a surviving
    event; a small banner persists across each event window. Every original
    frame is written exactly once so the duration is preserved.

    OpenCV writes a ``mp4v`` intermediate (reliable, but Chrome plays it poorly);
    it is then re-encoded to browser-friendly H.264 with ffmpeg when available
    (system or the bundled ``imageio-ffmpeg``). Returns the output path, or
    ``None`` if the video could not be re-opened for writing.
    """
    import os

    import cv2

    from .face_tracker import _reencode_h264, _safe_remove

    meta = read_metadata(video_path)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        return None

    # Write mp4v first; ffmpeg transcodes it to H.264 afterwards. avc1 is avoided
    # because the OpenH264 library it needs is often broken on Windows.
    raw_path = f'{output_path}.raw.mp4'
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(
        str(raw_path), fourcc, meta.fps or 30.0, (meta.width, meta.height)
    )

    by_frame = _surviving_detections_by_frame(detections, events)

    try:
        frame_number = 0
        while True:
            grabbed, frame = capture.read()
            if not grabbed:
                break

            timestamp = frame_number / meta.fps if meta.fps else 0.0
            active = [e for e in events if e.start_sec <= timestamp <= e.end_sec]

            for det in by_frame.get(frame_number, ()):  # boxes on sampled frames
                _draw_detection(cv2, frame, det)
            if active:
                _draw_event_banner(cv2, frame, active)

            writer.write(frame)
            frame_number += 1
    finally:
        capture.release()
        writer.release()

    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        _safe_remove(raw_path)
        return None

    if _reencode_h264(raw_path, str(output_path)):
        _safe_remove(raw_path)
    else:
        # No ffmpeg (or it failed): keep the OpenCV mp4v output at the final path.
        os.replace(raw_path, str(output_path))
    return str(output_path)


def _draw_detection(cv2, frame, det: FrameDetection) -> None:
    color = config.BEHAVIOR_COLORS.get(det.behavior_type, (0, 255, 0))
    x1, y1, x2, y2 = (int(v) for v in det.bbox)
    if (x2 - x1) > 0 and (y2 - y1) > 0:
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    label = f'{det.behavior_type} {det.confidence:.2f}'
    cv2.putText(
        frame, label, (x1, max(0, y1 - 8)),
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA,
    )


def _draw_event_banner(cv2, frame, active_events: list[AlertEvent]) -> None:
    labels = ', '.join(sorted({e.behavior_type for e in active_events}))
    cv2.putText(
        frame, f'ALERT: {labels}', (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA,
    )


def analyze_video(
    video_path: str,
    *,
    sample_every_n: int = config.SAMPLE_EVERY_N_FRAMES,
    object_confidence: float = config.OBJECT_CONFIDENCE,
    annotate: bool = True,
    output_path: str | None = None,
    object_detector: ObjectDetector | None = None,
    pose_analyzer: PoseAnalyzer | None = None,
) -> AnalysisResult:
    """Analyse a video end to end and return consolidated events.

    Parameters
    ----------
    video_path:
        Filesystem path to the source recording.
    sample_every_n:
        Frame-sampling stride (defaults to the configured rate).
    object_confidence:
        Minimum confidence for object detections.
    annotate:
        When ``True`` (default) an annotated MP4 is written next to the source
        (or to *output_path*) and its path is returned in the result.
    object_detector / pose_analyzer:
        Optional pre-built detectors (handy for tests or for reusing loaded
        models across many videos). Created on demand otherwise.

    Returns
    -------
    AnalysisResult
        Consolidated events, the annotated-video path (or ``None``), and
        processing metadata (per-type counts, frames analysed, elapsed time).
    """
    started = time.perf_counter()

    object_detector = object_detector or ObjectDetector(confidence=object_confidence)
    pose_analyzer = pose_analyzer or PoseAnalyzer()

    meta = read_metadata(video_path)
    detections, frames_analyzed = _collect_frame_detections(
        video_path, object_detector, pose_analyzer, sample_every_n
    )
    events = consolidate_events(detections)

    annotated_path = None
    if annotate and events:
        annotated_path = _write_annotated_video(
            video_path,
            detections,
            events,
            output_path or _default_output_path(video_path),
        )

    events_by_type: dict[str, int] = {}
    for event in events:
        events_by_type[event.behavior_type] = (
            events_by_type.get(event.behavior_type, 0) + 1
        )

    processing_time = round(time.perf_counter() - started, 3)
    metadata = {
        'events_by_type': events_by_type,
        'total_events': len(events),
        'frames_analyzed': frames_analyzed,
        'raw_detections': len(detections),
        'sample_every_n': sample_every_n,
        'processing_time_seconds': processing_time,
        'video': {
            'fps': meta.fps,
            'total_frames': meta.total_frames,
            'duration_seconds': meta.duration_sec,
            'width': meta.width,
            'height': meta.height,
        },
    }

    return AnalysisResult(
        events=events,
        annotated_video_path=annotated_path,
        metadata=metadata,
    )

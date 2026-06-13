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


def consolidate_events(
    detections: list[FrameDetection],
    merge_window_sec: float = config.MERGE_WINDOW_SEC,
    min_duration_sec: float = config.MIN_EVENT_DURATION_SEC,
) -> list[AlertEvent]:
    """Merge frame detections into events via the temporal rules layer.

    Same-class detections whose gap is within *merge_window_sec* collapse into
    one event; the event's confidence is the max across its frames. Events
    shorter than *min_duration_sec* are discarded as noise. Returns events
    sorted by start time.
    """
    events: list[AlertEvent] = []

    by_type: dict[str, list[FrameDetection]] = {}
    for det in detections:
        by_type.setdefault(det.behavior_type, []).append(det)

    for behavior_type, items in by_type.items():
        items.sort(key=lambda d: d.timestamp_sec)
        current: AlertEvent | None = None
        last_ts = None
        for det in items:
            if current is None or (det.timestamp_sec - last_ts) > merge_window_sec:
                if current is not None:
                    events.append(current)
                current = AlertEvent(
                    behavior_type=behavior_type,
                    confidence=det.confidence,
                    start_sec=det.timestamp_sec,
                    end_sec=det.timestamp_sec,
                    frame_count=1,
                )
            else:
                current.end_sec = det.timestamp_sec
                current.confidence = max(current.confidence, det.confidence)
                current.frame_count += 1
            last_ts = det.timestamp_sec
        if current is not None:
            events.append(current)

    survivors = [e for e in events if e.duration_sec >= min_duration_sec]
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
    frame is written exactly once so the duration is preserved. Returns the
    output path, or ``None`` if the video could not be re-opened for writing.
    """
    import cv2

    meta = read_metadata(video_path)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        return None

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(
        str(output_path), fourcc, meta.fps or 30.0, (meta.width, meta.height)
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

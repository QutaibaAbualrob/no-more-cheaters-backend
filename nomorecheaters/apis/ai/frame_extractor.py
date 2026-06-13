"""Video frame reader built on OpenCV.

Opens an uploaded recording, exposes its metadata (FPS / frame count /
duration), and yields sampled frames together with their frame number and
timestamp. Sampling keeps the pipeline tractable: at the default rate only
every 15th frame is decoded and handed to the detectors.

OpenCV handles the common container formats (mp4, mov, avi, mkv, …) through
its bundled FFmpeg, so callers do not need to special-case the extension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from . import config

# Fallback FPS for files that report 0 (some webcam/screen recordings do).
_DEFAULT_FPS = 30.0


@dataclass
class VideoMetadata:
    """Container-level facts about a video file."""

    fps: float
    total_frames: int
    duration_sec: float
    width: int
    height: int


@dataclass
class FrameSample:
    """A single decoded frame selected by the sampler.

    ``frame`` is a BGR ``numpy.ndarray`` (OpenCV's native layout).
    """

    frame: object
    frame_number: int
    timestamp_sec: float


def _open(path: str):
    """Open a video file, raising a clear error if it cannot be read."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f'Could not open video file: {path}')
    return capture


def _read_metadata(capture) -> VideoMetadata:
    import cv2

    fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
    if fps <= 0:
        fps = _DEFAULT_FPS
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration_sec = (total_frames / fps) if fps else 0.0
    return VideoMetadata(
        fps=fps,
        total_frames=total_frames,
        duration_sec=duration_sec,
        width=width,
        height=height,
    )


def read_metadata(path: str) -> VideoMetadata:
    """Return :class:`VideoMetadata` for *path* without decoding every frame."""
    capture = _open(path)
    try:
        return _read_metadata(capture)
    finally:
        capture.release()


def iter_sampled_frames(
    path: str,
    sample_every_n: int = config.SAMPLE_EVERY_N_FRAMES,
) -> Iterator[FrameSample]:
    """Yield every *sample_every_n*-th frame of the video at *path*.

    Each yielded :class:`FrameSample` carries the frame's absolute index and
    its timestamp in seconds (derived from the reported FPS). The capture is
    always released, even if the consumer stops iterating early.
    """
    sample_every_n = max(1, int(sample_every_n))
    capture = _open(path)
    try:
        meta = _read_metadata(capture)
        frame_number = 0
        while True:
            grabbed, frame = capture.read()
            if not grabbed:
                break
            if frame_number % sample_every_n == 0:
                yield FrameSample(
                    frame=frame,
                    frame_number=frame_number,
                    timestamp_sec=frame_number / meta.fps if meta.fps else 0.0,
                )
            frame_number += 1
    finally:
        capture.release()

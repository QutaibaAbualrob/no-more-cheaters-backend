"""YOLO-based video analysis package for No More Cheaters.

Detects exam-integrity events (phones, laptops, looking away) in recorded
exam videos and produces consolidated alert events plus an annotated video.

Public entry point::

    from apis.ai import analyze_video
    result = analyze_video('/path/to/video.mp4')
    for event in result.events:
        ...  # event.behavior_type, event.confidence, event.timestamp_sec

Heavy dependencies (OpenCV, ultralytics/torch) and the model weights are
loaded lazily on first use, so importing this package stays cheap.
"""

from __future__ import annotations

from . import config
from .detector import AlertEvent, AnalysisResult, analyze_video, consolidate_events

__all__ = [
    'analyze_video',
    'consolidate_events',
    'AnalysisResult',
    'AlertEvent',
    'config',
]

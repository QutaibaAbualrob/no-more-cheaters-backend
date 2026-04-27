from __future__ import annotations

import hashlib
import random

from .models import GlobalThresholdSettings, Video


def generate_fake_analysis(*, video: Video) -> tuple[str, list[str], dict]:
    """
    Deterministic (seeded) fake analysis so the UI can be exercised end-to-end.
    Replace this with real AI inference in production.
    """
    global_thresholds = GlobalThresholdSettings.get_solo()

    seed_bytes = hashlib.sha256(video.sha256.encode("utf-8")).digest()
    seed = int.from_bytes(seed_bytes[:8], "big", signed=False)
    rng = random.Random(seed)

    students_total = rng.randint(10, 30)
    suspicious_count = rng.randint(0, min(5, students_total))
    cheating_students = [f"Student {rng.randint(1, 40):02d}" for _ in range(suspicious_count)]

    suspicion_score = min(0.99, rng.random() * 0.9 + 0.05)
    summary = (
        "No suspicious behavior detected."
        if not cheating_students
        else f"Detected {len(cheating_students)} suspicious students in the session."
    )

    details = {
        "suspicion_score": suspicion_score,
        "students_total": students_total,
        "students_flagged": len(cheating_students),
        "thresholds_used": {
            "gaze_threshold": global_thresholds.gaze_threshold,
            "noise_threshold": global_thresholds.noise_threshold,
            "multiple_faces_threshold": global_thresholds.multiple_faces_threshold,
        },
        "signals": {
            "gaze": rng.random(),
            "noise": rng.random(),
            "multiple_faces": rng.random(),
        },
    }

    return summary, cheating_students, details

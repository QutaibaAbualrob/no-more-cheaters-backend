import random
from django.utils import timezone

from audit.utils import log_audit
from proctoring.models import Alert, AnalysisJob, AnalysisReport, SystemSettings


def run_analysis_job(job_id: int, user_id=None):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        job = AnalysisJob.objects.select_related("video").get(pk=job_id)
    except AnalysisJob.DoesNotExist:
        return

    job.status = AnalysisJob.Status.PROCESSING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    video = job.video
    settings_obj = SystemSettings.get()
    threshold = settings_obj.alert_threshold

    try:
        cheat_probability = round(random.uniform(0.15, 0.95), 4)
        if cheat_probability < 0.3:
            cheat_probability = 0.12

        events = []
        if cheat_probability >= threshold:
            events.append(
                {
                    "t": 12.5,
                    "type": "PHONE_DETECTED",
                    "confidence": 0.82,
                    "evidence": "Stub: object resembling phone in frame",
                }
            )
        if cheat_probability >= threshold:
            events.append(
                {
                    "t": 45.0,
                    "type": "LOOKING_AWAY",
                    "confidence": 0.71,
                    "evidence": "Stub: gaze away from screen",
                }
            )

        raw = {
            "cheat_probability": cheat_probability,
            "events": events,
            "stub": True,
        }

        report = AnalysisReport.objects.create(
            video=video,
            job=job,
            cheat_probability=cheat_probability,
            raw_json=raw,
        )

        for ev in events:
            if ev.get("confidence", 0) >= threshold:
                Alert.objects.create(
                    report=report,
                    timestamp_seconds=float(ev["t"]),
                    alert_type=ev["type"],
                    confidence=float(ev["confidence"]),
                    evidence=ev.get("evidence", ""),
                )

        job.status = AnalysisJob.Status.COMPLETED
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "completed_at"])

        u = None
        if user_id:
            u = User.objects.filter(pk=user_id).first()
        if u:
            log_audit(
                u,
                "analysis_complete",
                "video",
                str(video.pk),
                request=None,
                details={"job_id": job.pk},
            )
    except Exception as exc:
        job.status = AnalysisJob.Status.FAILED
        job.error_message = str(exc)[:2000]
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        raise

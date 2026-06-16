"""Background jobs for the No More Cheaters analysis pipeline.

These callables are enqueued onto the django-rq ``default`` queue and executed
by an ``rqworker`` process (or synchronously in-process when ``RQ_ASYNC`` is
off — the dev/test default). They own the :class:`~apis.models.AnalysisJob`
lifecycle (``QUEUED → PROCESSING → COMPLETED / FAILED``) and delegate the
actual detection work to the service layer (the YOLO/OpenCV pipeline behind
:func:`apis.services.build_ai_report`) so the orchestration here stays the same
regardless of how the analysis itself is implemented.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import AnalysisJob, AuditLog, ExamSession, Notification
from .services import build_ai_report, create_notification, record_audit_log


User = get_user_model()

logger = logging.getLogger(__name__)


def _existing_report_id(job):
    """Return the str id of the session's report, or ``None`` if not generated yet."""
    report = getattr(job.session, 'report', None)
    return str(report.id) if report is not None else None


def run_analysis(job_id, actor_id=None):
    """Run analysis for a queued :class:`AnalysisJob`.

    Marks the job ``PROCESSING``, runs the YOLO/OpenCV pipeline via
    :func:`apis.services.build_ai_report` to produce the Alerts/Report, then
    marks the job and its session ``COMPLETED`` and writes the completion audit
    entries. On any failure the job and session are flipped to ``FAILED`` with
    the error recorded, and the exception is re-raised so the worker registers
    the failure.

    Parameters
    ----------
    job_id:
        The :class:`AnalysisJob` primary key (str/UUID).
    actor_id:
        Optional id of the user who triggered the analysis, attributed to the
        completion audit entries. ``None`` when the job has no human actor.
    """
    # Atomically claim the job before doing any work: lock the row, and bail out
    # if it is already PROCESSING or COMPLETED. This stops a duplicate enqueue or
    # a second worker from re-running a finished analysis — which would delete
    # the first run's alerts, orphan its evidence files, and overwrite its report
    # (C7). QUEUED and FAILED jobs are claimable (FAILED so a retry can proceed).
    with transaction.atomic():
        job = (
            AnalysisJob.objects
            .select_for_update()
            .select_related('session', 'session__video', 'session__exam')
            .get(id=job_id)
        )
        if job.status in (AnalysisJob.Status.PROCESSING, AnalysisJob.Status.COMPLETED):
            logger.info(
                'run_analysis skipped: job %s already %s', job_id, job.status,
            )
            return _existing_report_id(job)

        job.status = AnalysisJob.Status.PROCESSING
        job.started_at = timezone.now()
        job.error_message = ''
        job.save(update_fields=['status', 'started_at', 'error_message'])

    session = job.session
    actor = User.objects.filter(id=actor_id).first() if actor_id else None

    try:
        with transaction.atomic():
            report = build_ai_report(session, job=job)
            job.status = AnalysisJob.Status.COMPLETED
            job.completed_at = timezone.now()
            job.save(update_fields=['status', 'completed_at'])
            session.status = ExamSession.Status.COMPLETED
            session.save(update_fields=['status', 'updated_at'])
    except Exception as exc:  # noqa: BLE001 — record failure then re-raise
        _mark_failed(job, session, str(exc))
        raise

    video_id = str(session.video.id) if hasattr(session, 'video') else str(session.id)
    record_audit_log(AuditLog.ActionType.ANALYSIS_COMPLETED, user=actor, target_resource=video_id)
    record_audit_log(AuditLog.ActionType.REPORT_GENERATED, user=actor, target_resource=str(report.id))

    # Tell the exam's instructor their report is ready.
    instructor = session.exam.instructor
    if instructor is not None:
        create_notification(
            instructor,
            Notification.NotifType.EXAM_UPDATED,
            f'Analysis complete for {session.exam.name}',
            f'{report.total_alerts} alerts detected. View the full report.',
            metadata={'session_id': str(session.id)},
        )
    return str(report.id)


def _mark_failed(job, session, error_message):
    """Flip a job and its session to FAILED, persisting the error message.

    Runs outside the rolled-back analysis transaction so the failure state is
    durably recorded even though the partial work was discarded.
    """
    job.status = AnalysisJob.Status.FAILED
    job.error_message = error_message
    job.completed_at = timezone.now()
    job.save(update_fields=['status', 'error_message', 'completed_at'])
    session.status = ExamSession.Status.FAILED
    session.save(update_fields=['status', 'updated_at'])

import platform
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    Alert, AnalysisJob, AuditLog, Exam, ExamSession, Notification, Report,
    SystemSettings, UserPreferences, Video, WorkspaceInvite,
)
from .selectors import is_admin, owned_reports, owned_sessions, owned_videos, recent_day_window


User = get_user_model()

THRESHOLD_DEFAULTS = {
    'gaze_threshold': 0.65,
    'noise_threshold': 0.7,
    'multiple_faces_threshold': 0.8,
}
THRESHOLD_DESCRIPTIONS = {
    'gaze_threshold': 'Confidence threshold for looking-away detections.',
    'noise_threshold': 'Confidence threshold for suspicious-audio detections.',
    'multiple_faces_threshold': 'Confidence threshold for multiple-face detections.',
}


def get_client_ip(request):
    """Extract the best available client IP for audit logging."""
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record_audit_log(action, *, user=None, target_resource='', metadata=None,
                     ip_address=None, user_agent=''):
    """Create an immutable audit entry without requiring an HTTP request.

    Used by background workers (which have no request object) and by
    :func:`write_audit_log`, which adapts a DRF request into these fields.
    """
    return AuditLog.objects.create(
        user=user,
        action=action,
        target_resource=target_resource,
        ip_address=ip_address,
        user_agent=user_agent or '',
        metadata=metadata or {},
    )


def write_audit_log(request, action, target_resource='', metadata=None):
    """Create an immutable audit entry for security-relevant actions."""
    return record_audit_log(
        action,
        user=request.user if request.user.is_authenticated else None,
        target_resource=target_resource,
        metadata=metadata,
        ip_address=get_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
    )


def create_notification(recipient, notif_type, title, body='', metadata=None):
    """Create an in-app notification for *recipient* and return it.

    Notifications live in the database only (never client storage) and are
    surfaced by the frontend bell icon. Used by flows such as workspace-invite
    assignment and invite responses.
    """
    return Notification.objects.create(
        recipient=recipient,
        notif_type=notif_type,
        title=title,
        body=body or '',
        metadata=metadata or {},
    )


def send_workspace_invite(dean, instructor, exam):
    """Create a workspace invite, email the instructor, and notify them in-app.

    The email contains accept/decline links that point at the frontend
    (``FRONTEND_URL``); the instructor does not need to be logged in to respond.
    Returns the created :class:`WorkspaceInvite`.
    """
    invite = WorkspaceInvite.objects.create(dean=dean, instructor=instructor, exam=exam)

    frontend = settings.FRONTEND_URL.rstrip('/')
    accept_url = f'{frontend}/invite/{invite.token}/accept'
    decline_url = f'{frontend}/invite/{invite.token}/decline'
    message = (
        'Hello,\n\n'
        f'{dean.email} has assigned you to supervise the exam "{exam.name}" '
        'on the No More Cheaters platform.\n\n'
        f'Accept:  {accept_url}\n'
        f'Decline: {decline_url}\n\n'
        'If you did not expect this invitation you can safely ignore this email.\n'
    )
    send_mail(
        subject=f'Invitation to supervise "{exam.name}"',
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[instructor.email],
        fail_silently=False,
    )

    create_notification(
        instructor,
        Notification.NotifType.EXAM_ASSIGNED,
        f'Assigned to {exam.name}',
        f'{dean.email} invited you to supervise "{exam.name}".',
        metadata={'invite_id': str(invite.id), 'exam_id': str(exam.id)},
    )
    return invite


def respond_to_workspace_invite(invite, accepted):
    """Record an accept/decline response and notify the dean.

    Idempotent: once an invite has been responded to, repeat calls leave it
    unchanged and send no further notifications.
    """
    if invite.status != WorkspaceInvite.Status.PENDING:
        return invite

    invite.status = (
        WorkspaceInvite.Status.ACCEPTED if accepted else WorkspaceInvite.Status.DECLINED
    )
    invite.responded_at = timezone.now()
    invite.save(update_fields=['status', 'responded_at'])

    verb = 'accepted' if accepted else 'declined'
    notif_type = (
        Notification.NotifType.INVITE_ACCEPTED
        if accepted
        else Notification.NotifType.INVITE_DECLINED
    )
    create_notification(
        invite.dean,
        notif_type,
        f'Invite {verb}',
        f'{invite.instructor.email} {verb} the invite to "{invite.exam.name}".',
        metadata={'invite_id': str(invite.id), 'exam_id': str(invite.exam_id)},
    )
    return invite


def threshold_payload(values, updated_at=None):
    """Normalise threshold values to the frontend API contract."""
    timestamp = (updated_at or timezone.now()).isoformat()
    return {
        'gaze_threshold': float(values.get('gaze_threshold', THRESHOLD_DEFAULTS['gaze_threshold'])),
        'noise_threshold': float(values.get('noise_threshold', THRESHOLD_DEFAULTS['noise_threshold'])),
        'multiple_faces_threshold': float(values.get(
            'multiple_faces_threshold',
            THRESHOLD_DEFAULTS['multiple_faces_threshold'],
        )),
        'updated_at': timestamp,
    }


def validate_thresholds(payload):
    """Accept only known threshold keys with numeric values between 0 and 1."""
    cleaned = {}
    for key, value in payload.items():
        if key not in THRESHOLD_DEFAULTS:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValidationError({key: 'Threshold must be a number.'})
        if numeric < 0 or numeric > 1:
            raise ValidationError({key: 'Threshold must be between 0 and 1.'})
        cleaned[key] = numeric
    if not cleaned:
        raise ValidationError('Submit at least one threshold value.')
    return cleaned


def get_global_thresholds():
    """Read global AI thresholds, creating defaults when missing."""
    values = {}
    updated_at = None
    for key, default in THRESHOLD_DEFAULTS.items():
        setting, _created = SystemSettings.objects.get_or_create(
            setting_key=key,
            defaults={
                'setting_value': str(default),
                'description': THRESHOLD_DESCRIPTIONS[key],
            },
        )
        values[key] = setting.setting_value
        if updated_at is None or setting.updated_at > updated_at:
            updated_at = setting.updated_at
    return threshold_payload(values, updated_at)


def get_user_thresholds(user):
    """Read a user's AI-threshold overrides from preferences metadata."""
    preferences, _created = UserPreferences.objects.get_or_create(user=user)
    thresholds = preferences.metadata.get('thresholds')
    if not thresholds:
        return None
    return threshold_payload(thresholds, preferences.updated_at)


def build_thresholds_response(user):
    """Merge global thresholds with optional per-user overrides."""
    global_thresholds = get_global_thresholds()
    user_thresholds = get_user_thresholds(user)
    effective = {**global_thresholds, **(user_thresholds or {})}
    return {
        'global': global_thresholds,
        'user': user_thresholds,
        'effective': threshold_payload(effective),
    }


def update_user_thresholds(user, payload):
    """Persist per-user threshold overrides."""
    cleaned = validate_thresholds(payload)
    preferences, _created = UserPreferences.objects.get_or_create(user=user)
    metadata = dict(preferences.metadata or {})
    current = dict(metadata.get('thresholds') or {})
    current.update(cleaned)
    metadata['thresholds'] = current
    preferences.metadata = metadata
    preferences.save(update_fields=['metadata', 'updated_at'])
    return threshold_payload(current, preferences.updated_at)


def update_global_thresholds(request, payload):
    """Persist global threshold settings and audit the change."""
    cleaned = validate_thresholds(payload)
    latest_updated_at = None
    for key, value in cleaned.items():
        setting, _created = SystemSettings.objects.update_or_create(
            setting_key=key,
            defaults={
                'setting_value': str(value),
                'description': THRESHOLD_DESCRIPTIONS[key],
                'updated_by': request.user,
            },
        )
        latest_updated_at = setting.updated_at
    write_audit_log(
        request,
        AuditLog.ActionType.SETTINGS_CHANGED,
        target_resource='ai_thresholds',
        metadata=cleaned,
    )
    return threshold_payload({**get_global_thresholds(), **cleaned}, latest_updated_at)


def get_available_upload_session(instructor, upload, exam_name='', student_identifier=''):
    """Create or reuse a direct-upload session that does not already have a video."""
    resolved_exam_name = exam_name or 'Uploaded Videos'
    resolved_student_identifier = student_identifier or Path(upload.name).stem or 'student'
    exam, _created = Exam.objects.get_or_create(
        instructor=instructor,
        name=resolved_exam_name,
        defaults={'description': 'Auto-created for direct video uploads.'},
    )
    base_identifier = resolved_student_identifier[:220]
    candidate = base_identifier
    suffix = 1

    while True:
        session, _created = ExamSession.objects.get_or_create(
            exam=exam,
            student_identifier=candidate,
        )
        if not hasattr(session, 'video'):
            return session
        suffix += 1
        candidate = f'{base_identifier}-{suffix}'


def recording_session_for(exam):
    """Return the single canonical recording :class:`ExamSession` for an exam.

    The calendar treats each exam as one recordable session, so video status,
    manual uploads, and auto recording all target this same row (create on first
    use).
    """
    session, _created = ExamSession.objects.get_or_create(
        exam=exam,
        student_identifier='__recording__',
    )
    return session


def resolve_calendar_exam(user, name):
    """Resolve a calendar entry name to a real, shared :class:`Exam` for *user*.

    Prefers an exam the user already owns or is an accepted supervisor of — so an
    invited instructor and the owning dean operate on the SAME exam — and only
    creates a new owned exam when nothing matches. This bridges the mock calendar
    entries to real, permission-checked backend records.
    """
    name = (name or '').strip() or 'Exam'
    owned = Exam.objects.filter(instructor=user, name=name).first()
    if owned is not None:
        return owned
    invited = (
        Exam.objects
        .filter(
            name=name,
            invites__instructor=user,
            invites__status=WorkspaceInvite.Status.ACCEPTED,
        )
        .first()
    )
    if invited is not None:
        return invited
    return Exam.objects.create(
        instructor=user,
        name=name,
        description='Created from the calendar for video analysis / recording.',
    )


def enqueue_analysis(request, video):
    """Queue a background analysis job for an uploaded video.

    Creates (or resets) the session's :class:`AnalysisJob` in the ``QUEUED``
    state, flips the session to ``PROCESSING``, records an ``ANALYSIS_STARTED``
    audit entry, and hands the job off to the django-rq ``default`` queue.

    The heavy work runs in :func:`apis.tasks.run_analysis`. When the ``default``
    queue is asynchronous (``RQ_ASYNC=true`` with Redis + an ``rqworker``) the
    job is handed off and this returns immediately while it is still ``QUEUED``.
    Otherwise (the dev/test default) the worker runs inline before this returns,
    so the job is already ``COMPLETED``. Callers should re-read the returned job
    to decide which case they are in.
    """
    # Local imports keep django-rq out of the import graph until it is used and
    # avoid a circular import with apis.tasks (which imports this module).
    import django_rq

    from .tasks import run_analysis

    session = video.session
    job, _created = AnalysisJob.objects.update_or_create(
        session=session,
        defaults={
            'status': AnalysisJob.Status.QUEUED,
            'ai_model_version': request.data.get('ai_model_version', 'yolo11x'),
            'frame_sample_rate': request.data.get('frame_sample_rate', 1),
            'started_at': None,
            'completed_at': None,
            'error_message': '',
            'metadata': {'source': 'api-ai-analysis'},
        },
    )
    session.status = ExamSession.Status.PROCESSING
    session.save(update_fields=['status', 'updated_at'])
    write_audit_log(request, AuditLog.ActionType.ANALYSIS_STARTED, target_resource=str(video.id))

    job_id = str(job.id)
    actor_id = str(request.user.id)
    queue = django_rq.get_queue('default')
    if queue.is_async:
        # Real worker + Redis: hand off and return; the job stays QUEUED here.
        queue.enqueue(run_analysis, job_id, actor_id=actor_id)
    else:
        # Eager fallback: run in-process so no Redis/worker is needed. rq's own
        # synchronous mode still persists job state to Redis, so we bypass it.
        run_analysis(job_id, actor_id=actor_id)

    job.refresh_from_db()
    return job


@transaction.atomic
def build_demo_report(session):
    """Produce deterministic demo Alerts + a Report for a session.

    This is intentionally isolated behind a service boundary so a future AI
    worker can replace it without changing the queue, views, serializers, or
    frontend API calls. It does not touch job status or audit logs — the
    caller (:func:`apis.tasks.run_analysis`) owns the job lifecycle.
    """
    alert, _alert_created = Alert.objects.get_or_create(
        session=session,
        timestamp_sec=30,
        behavior_type=Alert.BehaviorType.LOOKING_AWAY,
        defaults={
            'severity': Alert.Severity.MEDIUM,
            'confidence_score': 0.72,
            'metadata': {'reason': 'Demo analysis placeholder until AI worker is connected.'},
        },
    )
    alerts_by_type = dict(
        session.alerts.values('behavior_type').annotate(count=Count('id')).values_list('behavior_type', 'count')
    )
    total_alerts = sum(alerts_by_type.values())
    probability = min(0.99, round(total_alerts * 0.22 + alert.confidence_score * 0.4, 2))
    report, _report_created = Report.objects.update_or_create(
        session=session,
        defaults={
            'overall_cheating_probability': probability,
            'total_alerts': total_alerts,
            'alerts_by_type': alerts_by_type,
            'processing_time_seconds': 1.0,
            'summary': 'Demo analysis completed. Replace this workflow with the production AI pipeline.',
        },
    )
    return report


# Relative importance of each behaviour when combining detections into an
# overall cheating probability. Direct-evidence objects (a phone in hand)
# weigh more than soft cues (a turned head).
_BEHAVIOR_RISK_WEIGHTS = {
    Alert.BehaviorType.PHONE_DETECTED: 1.0,
    Alert.BehaviorType.LAPTOPS: 0.9,
    Alert.BehaviorType.MULTIPLE_FACES: 0.8,
    Alert.BehaviorType.OTHER_PERSON: 0.8,
    Alert.BehaviorType.OBJECT_DETECTED: 0.6,
    Alert.BehaviorType.LOOKING_AWAY: 0.4,
}


def _severity_for(confidence):
    """Bucket a detection confidence into an :class:`Alert.Severity`."""
    if confidence >= 0.8:
        return Alert.Severity.HIGH
    if confidence >= 0.5:
        return Alert.Severity.MEDIUM
    return Alert.Severity.LOW


def _cheating_probability(events):
    """Combine event confidences into an overall 0–1 cheating probability.

    Treats each event as an independent piece of (weighted) evidence and
    returns the probability that *at least one* is genuine:
    ``1 - ∏(1 - confidence·weight)``. More/stronger detections push the score
    up while a single soft cue keeps it modest. Capped at 0.99.
    """
    surviving_risk = 1.0
    for event in events:
        weight = _BEHAVIOR_RISK_WEIGHTS.get(event.behavior_type, 0.5)
        surviving_risk *= 1.0 - min(1.0, max(0.0, event.confidence) * weight)
    return round(min(0.99, 1.0 - surviving_risk), 2)


@transaction.atomic
def build_ai_report(session, job=None):
    """Run the real YOLO/OpenCV pipeline for *session* and persist findings.

    Drop-in replacement for :func:`build_demo_report`: same contract (takes a
    session, returns a :class:`Report`, leaves job status and audit logging to
    the caller) but backed by :func:`apis.ai.analyze_video`. Each consolidated
    detection event becomes an :class:`Alert`; the aggregate becomes the
    session's :class:`Report`. When *job* is supplied, the annotated-video path
    and analysis metadata are stored on it.

    Re-running replaces any prior alerts for the session so the report always
    reflects the latest analysis. Heavy dependencies (OpenCV, ultralytics) are
    imported lazily here so the service module stays cheap to import.
    """
    from apis.ai import analyze_video

    video = getattr(session, 'video', None)
    if video is None or not video.file:
        raise ValueError('Session has no video file to analyze.')

    result = analyze_video(video.file.path)

    # Replace prior detections so a re-analysis is not double-counted.
    session.alerts.all().delete()
    alerts = [
        Alert(
            session=session,
            timestamp_sec=event.timestamp_sec,
            behavior_type=event.behavior_type,
            severity=_severity_for(event.confidence),
            confidence_score=round(min(1.0, max(0.0, event.confidence)), 4),
            metadata={
                'source': 'ai-pipeline',
                'start_sec': round(event.start_sec, 3),
                'end_sec': round(event.end_sec, 3),
                'duration_sec': round(event.duration_sec, 3),
                'frame_count': event.frame_count,
            },
        )
        for event in result.events
    ]
    Alert.objects.bulk_create(alerts)

    alerts_by_type = dict(result.metadata.get('events_by_type', {}))
    total_alerts = len(alerts)
    probability = _cheating_probability(result.events)
    processing_time = result.metadata.get('processing_time_seconds')

    if total_alerts:
        summary = (
            f'AI analysis flagged {total_alerts} event(s) across '
            f'{result.metadata.get("frames_analyzed", 0)} sampled frame(s). '
            f'Overall cheating probability: {probability:.0%}.'
        )
    else:
        summary = (
            'AI analysis completed with no suspicious behaviour detected '
            f'across {result.metadata.get("frames_analyzed", 0)} sampled frame(s).'
        )

    report, _created = Report.objects.update_or_create(
        session=session,
        defaults={
            'overall_cheating_probability': probability,
            'total_alerts': total_alerts,
            'alerts_by_type': alerts_by_type,
            'processing_time_seconds': processing_time,
            'summary': summary,
        },
    )

    if job is not None:
        job_metadata = dict(job.metadata or {})
        job_metadata.update({
            'analysis': result.metadata,
            'annotated_video_path': result.annotated_video_path,
        })
        job.metadata = job_metadata
        job.save(update_fields=['metadata'])

    return report


def dashboard_stats_for(user):
    """Aggregate counts for dashboard cards using scoped querysets."""
    videos = owned_videos(user)
    sessions = owned_sessions(user)
    reports = owned_reports(user)
    return {
        'users_total': user.__class__.objects.count() if is_admin(user) else 1,
        'exams_total': Exam.objects.count() if is_admin(user) else Exam.objects.filter(instructor=user).count(),
        'alerts_total': Alert.objects.filter(session__in=sessions).count(),
        'videos_total': videos.count(),
        'videos_processing': sessions.filter(status=ExamSession.Status.PROCESSING).count(),
        'videos_completed': sessions.filter(status=ExamSession.Status.COMPLETED).count(),
        'analyses_total': AnalysisJob.objects.filter(session__in=sessions).count(),
        'cheating_reports_total': reports.filter(overall_cheating_probability__gte=0.5).count(),
    }


def activity_series_for(user):
    """Return seven-day upload and analysis counts for charts."""
    videos = owned_videos(user)
    analyses = AnalysisJob.objects.filter(session__in=owned_sessions(user))
    days, labels = recent_day_window()
    return {
        'days': labels,
        'videos': [videos.filter(uploaded_at__date=day).count() for day in days],
        'analyses': [analyses.filter(created_at__date=day).count() for day in days],
    }


def system_metrics():
    """Return lightweight runtime and object-count metrics."""
    return {
        'timestamp': timezone.now().isoformat(),
        'uptime_seconds': 0,
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'counts': {
            'users': User.objects.count(),
            'videos': Video.objects.count(),
            'analyses': AnalysisJob.objects.count(),
        },
    }

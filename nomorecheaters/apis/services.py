import logging
import platform
import shutil
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    Alert, AnalysisJob, AuditLog, AutoExamSession, Exam, ExamSession, Notification,
    Report, SystemSettings, UserPreferences, Video, Workspace, WorkspaceInvite,
    WorkspaceMembership,
)
from .selectors import is_admin, owned_reports, owned_sessions, owned_videos, recent_day_window


User = get_user_model()

logger = logging.getLogger(__name__)

THRESHOLD_DEFAULTS = {
    'gaze_threshold': 0.65,
    'noise_threshold': 0.7,
    'multiple_faces_threshold': 0.8,
}
THRESHOLD_DESCRIPTIONS = {
    'gaze_threshold': (
        'Detection sensitivity (0-1): the minimum confidence floor applied to '
        'both the object (phone/laptop) and looking-away detectors. Lower = more '
        'sensitive (flags borderline cases).'
    ),
    # Reserved: audio analysis is not implemented yet, so this value is accepted
    # and stored but does not affect analysis. See N3 in critical_problems.md.
    'noise_threshold': 'Reserved for future suspicious-audio detection (not yet active).',
    # Reserved: there is no multiple-face detector yet, so this value is accepted
    # and stored but does not affect analysis. See N3/C2 in critical_problems.md.
    'multiple_faces_threshold': 'Reserved for future multiple-face detection (not yet active).',
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


def email_workspace_invite(invite):
    """Send (or resend) the accept/decline email for an invite. Best-effort.

    Returns ``True`` when the email was sent, ``False`` when sending failed —
    the caller keeps the invite either way so a flaky mail server never blocks
    the workflow.
    """
    target = invite.target_name
    frontend = settings.FRONTEND_URL.rstrip('/')
    accept_url = f'{frontend}/invite/{invite.token}/accept'
    decline_url = f'{frontend}/invite/{invite.token}/decline'
    if invite.workspace_id:
        action_line = f'{invite.dean.email} has invited you to join the workspace "{target}"'
    else:
        action_line = f'{invite.dean.email} has assigned you to supervise the exam "{target}"'
    message = (
        'Hello,\n\n'
        f'{action_line} on the No More Cheaters platform.\n\n'
        f'Accept:  {accept_url}\n'
        f'Decline: {decline_url}\n\n'
        'If you did not expect this invitation you can safely ignore this email.\n'
    )
    try:
        send_mail(
            subject=f'Invitation to join "{target}"',
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invite.instructor.email],
            fail_silently=False,
        )
        return True
    except Exception:  # noqa: BLE001 — email is best-effort, never fatal
        logger.exception('Failed to send workspace invite email for invite %s', invite.id)
        return False


def send_workspace_invite(dean, instructor, exam=None, workspace=None):
    """Create an invite, email the invitee (best-effort), and notify them in-app.

    Targets a *workspace* (joining it on accept) and/or an *exam* (legacy
    supervisor assignment); at least one should be supplied. Returns the created
    :class:`WorkspaceInvite`. A failing mail server is logged but does not abort
    the invite — the row and notification are still saved.
    """
    invite = WorkspaceInvite.objects.create(
        dean=dean, instructor=instructor, exam=exam, workspace=workspace,
    )
    email_workspace_invite(invite)

    target = invite.target_name
    # dean_email + token are required by the frontend Alerts page so it can
    # render the sender and call accept/decline.
    metadata = {
        'invite_id': str(invite.id),
        'token': str(invite.token),
        'dean_email': dean.email,
    }
    if workspace is not None:
        metadata['workspace_id'] = str(workspace.id)
        metadata['workspace_name'] = workspace.name
        notif_type = Notification.NotifType.WORKSPACE_INVITE
        title = f'{dean.email} invited you to join workspace: {target}'
        body = 'Click to view the invite and accept or decline'
    else:
        if exam is not None:
            metadata['exam_id'] = str(exam.id)
        notif_type = Notification.NotifType.EXAM_ASSIGNED
        title = f'{dean.email} invited you to "{target}"'
        body = 'Click to view the invite and accept or decline'

    create_notification(instructor, notif_type, title, body, metadata=metadata)
    return invite


def exam_supervisor_users(exam, extra_user_ids=None):
    """Resolve the people who supervise *exam* and should be notified of changes.

    That is the exam owner, every instructor with an ACCEPTED invite for the
    exam, every instructor who scheduled an :class:`AutoExamSession` for it, plus
    any extra user ids the caller supplies (e.g. the calendar's selected
    supervisors). Returns a list of distinct :class:`User` objects.
    """
    ids = {exam.instructor_id}
    ids.update(
        WorkspaceInvite.objects
        .filter(exam=exam, status=WorkspaceInvite.Status.ACCEPTED)
        .values_list('instructor_id', flat=True)
    )
    ids.update(
        AutoExamSession.objects
        .filter(exam=exam)
        .values_list('instructor_id', flat=True)
    )
    for raw in (extra_user_ids or []):
        if raw:
            ids.add(raw)
    ids.discard(None)
    return list(User.objects.filter(id__in=ids))


def delete_exam_media(exam):
    """Delete all on-disk media for an exam's sessions before the DB cascade.

    Django's FK cascade removes the Video/Alert/Report rows but never the files
    they reference. This clears the uploaded video plus the per-session clip and
    snapshot directories under ``MEDIA_ROOT``. Best-effort: any IO error is
    logged and skipped so a locked/missing file never blocks the delete.
    """
    media_root = Path(settings.MEDIA_ROOT)
    for session in exam.sessions.all():
        video = getattr(session, 'video', None)
        if video is not None and video.file:
            try:
                video.file.delete(save=False)
            except Exception:  # noqa: BLE001 — best-effort file cleanup
                logger.exception('Failed to delete video file for session %s', session.id)
        # exam-videos/<session>, clips/<session>, snapshots/<session>
        for subdir in ('exam-videos', 'clips', 'snapshots'):
            shutil.rmtree(media_root / subdir / str(session.id), ignore_errors=True)


def clear_session_evidence(session):
    """Delete a session's on-disk evidence (clip + snapshot) directories.

    Evidence files are named by alert UUID, so a re-analysis writes a fresh set
    under new names and never overwrites the old ones — left alone, every re-run
    accumulates orphaned snapshots/<session>/ and clips/<session>/ artifacts on
    disk even though their Alert rows were deleted (C5). Called before the new
    evidence is written, so wiping the whole per-session directory is safe.
    Best-effort: any IO error is logged and skipped so a locked/missing file
    never blocks the analysis.
    """
    media_root = Path(settings.MEDIA_ROOT)
    for subdir in ('clips', 'snapshots'):
        target = media_root / subdir / str(session.id)
        try:
            shutil.rmtree(target, ignore_errors=True)
        except Exception:  # noqa: BLE001 — best-effort file cleanup
            logger.exception('Failed to clear %s for session %s', subdir, session.id)


def notify_users(users, notif_type, title, body, metadata=None):
    """Create an in-app notification for each user, then email them off-thread.

    The in-app notifications are written synchronously — they are fast, durable,
    and expected to be visible immediately. The email fan-out (the slow, flaky
    part) is handed to the django-rq ``default`` queue so a request resolving N
    supervisors never blocks on N sequential SMTP round-trips (H9). When the
    queue is eager (the dev/test default, no Redis/worker) the emails are sent
    inline, so behaviour is unchanged without a worker.
    """
    recipients = []
    for user in users:
        create_notification(user, notif_type, title, body, metadata=metadata)
        email = getattr(user, 'email', '')
        if email:
            recipients.append(email)

    if recipients:
        enqueue_email(recipients, title, body)


def enqueue_email(recipients, subject, body):
    """Fan *subject*/*body* out to *recipients*, off the HTTP thread when possible.

    Hands the SMTP loop to the django-rq ``default`` queue; falls back to sending
    inline when the queue is eager (no Redis/worker — the dev/test default).
    Mirrors :func:`enqueue_analysis`'s async/eager handling. The local imports
    keep django-rq out of the import graph until used and avoid a circular import
    with :mod:`apis.tasks` (which imports this module).
    """
    import django_rq

    from .tasks import send_notification_emails

    payload = list(recipients)
    queue = django_rq.get_queue('default')
    if queue.is_async:
        queue.enqueue(send_notification_emails, payload, subject, body)
    else:
        send_notification_emails(payload, subject, body)


def respond_to_workspace_invite(invite, accepted):
    """Record an accept/decline response, join the workspace, and notify the dean.

    Idempotent: once an invite has been responded to, repeat calls leave it
    unchanged and send no further notifications. Accepting a workspace invite
    creates a :class:`WorkspaceMembership` (no-op if one already exists).
    """
    if invite.status != WorkspaceInvite.Status.PENDING:
        return invite

    invite.status = (
        WorkspaceInvite.Status.ACCEPTED if accepted else WorkspaceInvite.Status.DECLINED
    )
    invite.responded_at = timezone.now()
    invite.save(update_fields=['status', 'responded_at'])

    if accepted and invite.workspace_id:
        WorkspaceMembership.objects.get_or_create(
            workspace=invite.workspace, instructor=invite.instructor,
        )

    verb = 'accepted' if accepted else 'declined'
    notif_type = (
        Notification.NotifType.INVITE_ACCEPTED
        if accepted
        else Notification.NotifType.INVITE_DECLINED
    )
    metadata = {'invite_id': str(invite.id)}
    if invite.workspace_id:
        metadata['workspace_id'] = str(invite.workspace_id)
    if invite.exam_id:
        metadata['exam_id'] = str(invite.exam_id)
    create_notification(
        invite.dean,
        notif_type,
        f'Invite {verb}',
        f'{invite.instructor.email} {verb} your invite to "{invite.target_name}".',
        metadata=metadata,
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
    """Accept only known threshold keys with numeric values between 0 and 1.

    Unknown keys are rejected rather than silently dropped (M3): a typo'd key
    (e.g. ``gaze_treshold``) used to be discarded, so the save "succeeded"
    while the intended threshold never changed. Surfacing it as a validation
    error lets the caller fix the key instead of trusting a phantom write.
    """
    cleaned = {}
    for key, value in payload.items():
        if key not in THRESHOLD_DEFAULTS:
            raise ValidationError(
                {key: f'Unknown threshold key. Allowed keys: '
                      f'{", ".join(sorted(THRESHOLD_DEFAULTS))}.'})
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


def effective_detection_confidence(user):
    """Resolve the active detection-confidence floor (0-1) for *user*.

    This is the bridge that makes the AIThresholds page real: it returns the
    effective ``gaze_threshold`` (the global default, overridden by the user's
    saved preference) so :func:`build_ai_report` can feed it to the AI pipeline
    as the minimum-confidence floor for *both* the object and looking-away
    detectors. Lower value → more sensitive.

    The ``noise_threshold`` and ``multiple_faces_threshold`` knobs are
    intentionally NOT consumed here: their features (audio analysis, multi-face
    detection) do not exist yet, so feeding them to the pipeline would be
    meaningless. They remain stored/returned as reserved settings.
    """
    effective = build_thresholds_response(user)['effective']
    try:
        value = float(effective.get('gaze_threshold', THRESHOLD_DEFAULTS['gaze_threshold']))
    except (TypeError, ValueError):
        value = THRESHOLD_DEFAULTS['gaze_threshold']
    # Clamp into the detector's valid range; 0 would disable filtering entirely.
    return min(1.0, max(0.0, value))


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
    # Frame-sampling stride: honour a client-supplied value, else fall back to
    # the model's (now truthful) default rather than a misleading 1 (H2/M9).
    # ai_model_version is no longer a client input — it is provenance the
    # pipeline stamps with the model it actually ran (H3), so it starts blank.
    default_rate = AnalysisJob._meta.get_field('frame_sample_rate').default
    try:
        sample_rate = int(request.data.get('frame_sample_rate', default_rate))
    except (TypeError, ValueError):
        sample_rate = default_rate
    if sample_rate < 1:
        sample_rate = default_rate

    job, _created = AnalysisJob.objects.update_or_create(
        session=session,
        defaults={
            'status': AnalysisJob.Status.QUEUED,
            'ai_model_version': '',
            'frame_sample_rate': sample_rate,
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
    Alert.BehaviorType.LOOKING_AWAY: 0.4,
}


def _severity_for(confidence):
    """Bucket a detection confidence into an :class:`Alert.Severity`."""
    if confidence >= 0.8:
        return Alert.Severity.HIGH
    if confidence >= 0.5:
        return Alert.Severity.MEDIUM
    return Alert.Severity.LOW


def _report_probability(alerts):
    """Overall cheating probability from independent weighted evidence.

    Each alert is treated as one independent piece of evidence contributing its
    behaviour-weighted confidence (``confidence_score · behaviour_weight``); the
    report score is the probability that *at least one* is a genuine violation:

        ``1 - ∏(1 - confidence·weight)``

    This fixes two defects in the old severity-mean formula:

    * **Monotonic (C6):** adding or strengthening evidence can only push the
      score up, never down. The old arithmetic mean diluted a serious alert as
      soon as minor ones were added (1 phone = 1.00, but 1 phone + 5 glances =
      0.42).
    * **Behaviour-aware (N1):** a phone (weight 1.0) genuinely outweighs a turned
      head (weight 0.4), and the raw ``confidence_score`` is used directly rather
      than a 3-bucket severity label. The old formula keyed on severity only, so
      a sustained head-turn that saturated to HIGH scored an identical 1.00 to a
      phone — fabricating cheating against honest students.

    Capped at 0.99 (independent evidence never proves certainty).
    """
    surviving_risk = 1.0
    for alert in alerts:
        weight = _BEHAVIOR_RISK_WEIGHTS.get(alert.behavior_type, 0.5)
        confidence = min(1.0, max(0.0, alert.confidence_score or 0.0))
        surviving_risk *= 1.0 - confidence * weight
    return round(min(0.99, 1.0 - surviving_risk), 2)


def _attach_alert_evidence(video, session, alerts, person_index=None):
    """Attach per-alert visual evidence with a green-boxed flagged person.

    *person_index* is the per-person track index the analysis pass already built
    from YOLO boxes (H6); it is reused as-is so the video is not scanned again.
    Only when it is ``None`` (a direct evidence call outside the pipeline) does
    this fall back to :func:`apis.ai.face_tracker.track_persons`, the standalone
    Haar-cascade sweep.

    For each alert it resolves the flagged person near its timestamp and saves
    three artifacts:

    * **crop** (``metadata['crop_url']``) — the flagged face only, no overlay;
      used as the per-person avatar in the report header.
    * **annotated frame** (``snapshot_url``) — the FULL frame with a green box +
      behaviour label on the flagged person and gray boxes on anyone else; the
      main evidence image so the flagged student is visible in context.
    * **clip** (``clip_url``) — a 3-second clip with the same green/gray overlay
      baked onto every frame.

    Best-effort and fully isolated: any OpenCV/IO failure simply leaves that
    artifact empty and never aborts the analysis. URLs are stored as
    ``/media/...`` web paths; the report view turns them into absolute URLs.

    Returns the number of alerts whose evidence was **incomplete** — i.e. an
    extractor raised (a disk-full or OpenCV error, as opposed to legitimately
    finding nothing to extract). Each such alert is also stamped with
    ``metadata['evidence_incomplete'] = True`` (and the failing artifact names in
    ``metadata['evidence_errors']``) so the partial state is visible rather than
    silently swallowed (M1). Each alert is processed independently, so a failure
    midway through never starves the remaining alerts of evidence.
    """
    if not alerts or not getattr(video, 'file', None):
        return 0

    try:
        from .ai.face_tracker import (
            extract_annotated_frame, extract_clip, extract_face_crop, track_persons,
        )
    except Exception:  # noqa: BLE001 — OpenCV missing → skip evidence entirely
        logger.warning('OpenCV/face_tracker unavailable; skipping evidence for session %s', session.id)
        return len(alerts)

    video_path = video.file.path
    media_root = Path(settings.MEDIA_ROOT)
    media_url = '/' + settings.MEDIA_URL.strip('/')

    # Reuse the pipeline's YOLO-derived index when present; otherwise sweep the
    # video once with the Haar fallback (H6).
    index = person_index
    if index is None:
        try:
            index = track_persons(video_path)
        except Exception:  # noqa: BLE001
            index = None

    # Person boxes (YOLO) need a head crop for the avatar; face boxes (Haar) are
    # used as-is.
    head_fraction = 0.45 if getattr(index, 'box_kind', 'face') == 'person' else None

    incomplete_alerts = 0
    for alert in alerts:
        # Each alert is fully isolated: an unexpected error here (a disk-full
        # OSError, a corrupt frame) marks just this alert incomplete and moves
        # on, so a failure partway through can never starve later alerts of
        # evidence the way an un-caught exception would (M1).
        failed_artifacts: list[str] = []
        try:
            person_id, bbox, others = 'person_1', None, {}
            if index is not None:
                try:
                    person_id, bbox = index.query(alert.timestamp_sec)
                    others = index.persons_at(alert.timestamp_sec)
                except Exception:  # noqa: BLE001
                    person_id, bbox, others = 'person_1', None, {}

            # Human-readable behaviour ("Phone Detected", "Looking Away", …).
            behavior_label = alert.get_behavior_type_display()

            # All people in the frame, with the flagged person's precise bbox.
            boxes = dict(others)
            if bbox is not None:
                boxes[person_id] = bbox
            boxes_list = list(boxes.items())

            metadata = dict(alert.metadata or {})
            metadata['person_id'] = person_id
            if bbox is not None:
                x1, y1, x2, y2 = bbox
                metadata['bbox'] = [
                    int(round(x1)), int(round(y1)),
                    int(round(x2 - x1)), int(round(y2 - y1)),
                ]

            crop_rel = f'snapshots/{session.id}/{alert.id}_crop.jpg'
            frame_rel = f'snapshots/{session.id}/{alert.id}_frame.jpg'
            clip_rel = f'clips/{session.id}/{alert.id}.mp4'

            # 1. Clean face crop → avatar (stored in metadata).
            try:
                if extract_face_crop(video_path, alert.timestamp_sec, bbox,
                                      str(media_root / crop_rel), head_fraction=head_fraction):
                    metadata['crop_url'] = f'{media_url}/{crop_rel}'
            except Exception:  # noqa: BLE001
                failed_artifacts.append('crop')
                logger.exception('Evidence crop failed for alert %s', alert.id)
            # 2. Full annotated frame → main evidence image (snapshot_url).
            try:
                if extract_annotated_frame(
                    video_path, alert.timestamp_sec, boxes_list, person_id,
                    behavior_label, str(media_root / frame_rel),
                ):
                    alert.snapshot_url = f'{media_url}/{frame_rel}'
            except Exception:  # noqa: BLE001
                failed_artifacts.append('snapshot')
                logger.exception('Evidence snapshot failed for alert %s', alert.id)
            # 3. 3-second clip with the overlay on every frame (clip_url).
            try:
                if extract_clip(
                    video_path, alert.timestamp_sec, str(media_root / clip_rel),
                    boxes=boxes_list, flagged_person_id=person_id, behavior_label=behavior_label,
                ):
                    alert.clip_url = f'{media_url}/{clip_rel}'
            except Exception:  # noqa: BLE001
                failed_artifacts.append('clip')
                logger.exception('Evidence clip failed for alert %s', alert.id)

            if failed_artifacts:
                metadata['evidence_incomplete'] = True
                metadata['evidence_errors'] = failed_artifacts
                incomplete_alerts += 1

            alert.metadata = metadata
        except Exception:  # noqa: BLE001 — one alert must never abort the rest
            logger.exception('Evidence generation aborted for alert %s', alert.id)
            aborted_metadata = dict(alert.metadata or {})
            aborted_metadata['evidence_incomplete'] = True
            alert.metadata = aborted_metadata
            incomplete_alerts += 1

    try:
        Alert.objects.bulk_update(alerts, ['snapshot_url', 'clip_url', 'metadata'])
    except Exception:  # noqa: BLE001 — persistence itself failed: nothing saved
        logger.exception('Failed to persist alert evidence for session %s', session.id)
        return len(alerts)

    return incomplete_alerts


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

    Transaction boundaries (C4): the multi-minute ``analyze_video`` pass and the
    per-alert evidence I/O (face tracking, JPEG/MP4 writes, ffmpeg) run OUTSIDE
    any database transaction. Only the quick row writes are wrapped in short
    ``atomic`` blocks, so a DB connection and row locks are never held while the
    CPU/GPU/ffmpeg work runs.
    """
    from .ai import analyze_video

    video = getattr(session, 'video', None)
    if video is None or not video.file:
        raise ValueError('Session has no video file to analyze.')

    # Bridge the AIThresholds sensitivity into the pipeline: the exam owner's
    # effective gaze_threshold becomes the minimum-confidence floor for both
    # detectors, so the slider actually controls detection (fixes C1).
    instructor = getattr(session.exam, 'instructor', None)
    confidence_floor = (
        effective_detection_confidence(instructor)
        if instructor is not None
        else THRESHOLD_DEFAULTS['gaze_threshold']
    )
    # Wire the job's frame-sampling stride into the pipeline (H2). Without a job
    # (e.g. a direct service call) analyze_video uses its own configured default.
    analyze_kwargs = {
        'object_confidence': confidence_floor,
        'pose_confidence': confidence_floor,
    }
    if job is not None and job.frame_sample_rate:
        analyze_kwargs['sample_every_n'] = job.frame_sample_rate
    result = analyze_video(video.file.path, **analyze_kwargs)

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
    # Persist the alert rows in a short transaction. Replacing prior detections
    # (so a re-analysis is not double-counted) and creating the new ones is the
    # only DB work here; the analyze_video pass above ran with no transaction
    # open, and the evidence I/O below runs with none open either (C4).
    with transaction.atomic():
        session.alerts.all().delete()
        Alert.objects.bulk_create(alerts)

    # The deleted alerts' evidence files (named by old alert UUID) would survive
    # the row delete above; wipe the per-session clip/snapshot dirs before the
    # new evidence is written so re-runs don't accumulate orphans on disk (C5).
    # File I/O, so it runs outside any transaction (C4).
    clear_session_evidence(session)

    # Enrich each alert with a face crop, a 3-second clip, and a person id. This
    # is filesystem/ffmpeg I/O (minutes for long videos) and deliberately runs
    # OUTSIDE a transaction; it commits its own per-alert bulk_update at the end.
    # Reuse the person index the analysis pass already built from YOLO boxes (H6)
    # so the video is not swept a third time.
    incomplete_evidence = _attach_alert_evidence(
        video, session, alerts, person_index=getattr(result, 'person_index', None),
    )
    if incomplete_evidence:
        logger.warning(
            'Evidence incomplete for %d of %d alert(s) in session %s',
            incomplete_evidence, len(alerts), session.id,
        )

    # Aggregate from the persisted alerts (now enriched with person ids), so the
    # report reflects exactly what was saved.
    alerts_by_type = {}
    person_ids = set()
    for alert in alerts:
        alerts_by_type[alert.behavior_type] = alerts_by_type.get(alert.behavior_type, 0) + 1
        person_id = (alert.metadata or {}).get('person_id')
        if person_id:
            person_ids.add(person_id)

    total_alerts = len(alerts)
    # Number of *distinct* people the alerts were attributed to. This is only
    # known when person tracking/evidence actually ran and stamped a person_id
    # onto the alert metadata; if that failed entirely, person_ids is empty and
    # the count is genuinely unknown — we must not fabricate "1 person" (M12),
    # which previously happened via a ``len(...) or 1`` fallback and made the
    # report claim someone was identified when no one was.
    person_count = len(person_ids)
    probability = _report_probability(alerts)
    processing_time = result.metadata.get('processing_time_seconds')

    if not total_alerts:
        summary = 'No suspicious activity detected in this session.'
    elif person_count:
        summary = f'{total_alerts} alert(s) detected across {person_count} person(s).'
    else:
        summary = (
            f'{total_alerts} alert(s) detected; the number of people involved '
            'could not be determined.'
        )

    # Persist the aggregate report (and the job's analysis metadata) in a short
    # transaction — again, no I/O is held open here (C4).
    with transaction.atomic():
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
                'annotated_video_url': _media_url_for(result.annotated_video_path),
                # Surface partial-evidence runs so the frontend can flag that some
                # alerts are missing their snapshot/clip rather than implying the
                # absence means "nothing happened" (M1).
                'evidence_incomplete': bool(incomplete_evidence),
                'evidence_incomplete_count': incomplete_evidence,
            })
            job.metadata = job_metadata
            # Stamp the model that actually ran (H3): provenance, not a client
            # input. The object-detection weights identify the run.
            object_model = ((result.metadata or {}).get('model') or {}).get('object')
            update_fields = ['metadata']
            if object_model:
                job.ai_model_version = str(object_model)[:100]
                update_fields.append('ai_model_version')
            job.save(update_fields=update_fields)

    return report


def _media_url_for(filesystem_path):
    """Turn an absolute path under ``MEDIA_ROOT`` into a ``/media/...`` web URL.

    Used to expose the annotated analysis video (written next to the source
    upload) to the frontend. Returns ``None`` for a missing path or one that
    falls outside ``MEDIA_ROOT``.
    """
    if not filesystem_path:
        return None
    try:
        rel = Path(filesystem_path).resolve().relative_to(Path(settings.MEDIA_ROOT).resolve())
    except (ValueError, OSError):
        return None
    return '/' + settings.MEDIA_URL.strip('/') + '/' + str(rel).replace('\\', '/')


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
    """Return seven-day upload and analysis counts for charts.

    Two grouped queries — one per series — instead of the old 2·N per-day
    ``COUNT`` queries (H7). Each truncates the timestamp to a local date, groups,
    and counts in the database; the rows are then mapped onto the fixed day
    window, with any day that has no rows reported as 0.
    """
    videos = owned_videos(user)
    analyses = AnalysisJob.objects.filter(session__in=owned_sessions(user))
    days, labels = recent_day_window()
    start = days[0]  # window is oldest -> newest, so days[0] is the lower bound

    video_counts = _daily_counts(videos, 'uploaded_at', start)
    analysis_counts = _daily_counts(analyses, 'created_at', start)
    return {
        'days': labels,
        'videos': [video_counts.get(day, 0) for day in days],
        'analyses': [analysis_counts.get(day, 0) for day in days],
    }


def _daily_counts(queryset, field_name, start_date):
    """Return ``{date: row_count}`` for *field_name*, grouped by day in one query.

    Only rows on/after *start_date* are scanned, and ``order_by()`` clears any
    model default ordering so it cannot leak into (and break) the ``GROUP BY``.
    The truncation uses the active timezone, matching ``recent_day_window``'s
    use of :func:`~django.utils.timezone.localdate`.
    """
    rows = (
        queryset
        .filter(**{f'{field_name}__date__gte': start_date})
        .annotate(_day=TruncDate(field_name))
        .order_by()
        .values('_day')
        .annotate(_count=Count('pk'))
    )
    return {row['_day']: row['_count'] for row in rows}


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

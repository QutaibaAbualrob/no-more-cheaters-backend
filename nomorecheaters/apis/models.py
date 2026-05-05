"""
Domain models for the No More Cheaters exam-proctoring system.

This module defines the core data layer used by the REST API.  Each model
links back to one or more functional requirements (FR) from the project
documentation.

Model hierarchy::

    User (custom, email-based auth)
     └── Exam
          └── ExamSession  (one per student per exam)
               ├── Video         (1:1 — uploaded recording)
               ├── AnalysisJob   (1:1 — AI processing tracker)
               ├── Alert         (1:N — flagged incidents)
               └── Report        (1:1 — aggregated results)

    AuditLog        — immutable action log  (FR14)
    SystemSettings  — admin-configurable key/value pairs  (FR13)
"""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def video_upload_to(instance: "Video", filename: str) -> str:
    """Return the upload path for a video file, organised by session ID.

    Files are stored under ``exam-videos/<session-uuid>/<filename>`` so that
    each exam session's recordings are grouped together on disk or in cloud
    storage.
    """
    return f"exam-videos/{instance.session_id}/{filename}"


class User(AbstractUser):
    """Custom user model for the No More Cheaters platform.

    Extends Django's ``AbstractUser`` with:
    * A UUID primary key (instead of auto-incrementing integer).
    * Email as the login identifier (``USERNAME_FIELD = 'email'``).
    * A required ``role`` field — either **ADMIN** or **INSTRUCTOR**.

    Implements **FR1** (secure login for instructors and administrators).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, null=False, blank=False)

    class Role(models.TextChoices):
        ADMIN = 'ADMIN', 'Administrator'
        INSTRUCTOR = 'INSTRUCTOR', 'Instructor'

    role = models.CharField(max_length=20, choices=Role.choices, null=False, blank=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'role']

    def __str__(self):
        return f"{self.email} ({self.role})"


class Exam(models.Model):
    """An exam created by an instructor.

    Acts as a logical container for one or more :class:`ExamSession` instances.
    Each exam belongs to a single instructor (or admin).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='exams',
        limit_choices_to={'role__in': [User.Role.ADMIN, User.Role.INSTRUCTOR]},
    )
    name = models.CharField(max_length=255, null=False, blank=False)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['instructor', 'created_at']),
        ]

    def __str__(self):
        return self.name


class ExamSession(models.Model):
    """A single student's recorded session within an exam.

    Tracks the lifecycle of the video from upload through AI analysis:
    ``PENDING → PROCESSING → COMPLETED`` (or ``FAILED``).

    A unique constraint ensures one session per student per exam.
    Implements **FR5** (process uploaded videos offline).
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PROCESSING = 'PROCESSING', 'Processing'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='sessions')
    student_identifier = models.CharField(max_length=255, null=False, blank=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['exam', 'status', 'created_at']),
            models.Index(fields=['student_identifier']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['exam', 'student_identifier'],
                name='unique_student_session_per_exam',
            ),
        ]

    @property
    def instructor(self):
        """Shortcut to the instructor who owns the parent exam."""
        return self.exam.instructor

    def __str__(self):
        return f"{self.exam.name} - {self.student_identifier}"


class Video(models.Model):
    """An uploaded exam-session video recording.

    Each session has at most one video (``OneToOneField``).  On upload the
    serializer computes a SHA-256 hash of the file content; the ``file_hash``
    field is unique to **prevent duplicate uploads** (FR4).  The ``expires_at``
    field supports the 30-day video-retention policy described in the privacy
    design.

    Implements **FR2** (upload recorded exam videos), **FR3** (validate video
    format and integrity), and **FR4** (prevent duplicate uploads).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ExamSession, on_delete=models.CASCADE, related_name='video')
    file = models.FileField(upload_to=video_upload_to)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    file_hash = models.CharField(max_length=64, unique=True, null=False, blank=False)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Auto-delete video after this date.',
    )

    class Meta:
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['uploaded_at']),
            models.Index(fields=['file_hash']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"Video for {self.session.exam.name}"


class Alert(models.Model):
    """A single suspicious-behaviour incident detected by the AI module.

    Each alert records the **type** of behaviour, a **confidence score**
    (0.0–1.0), the **timestamp** within the video, and an optional
    ``snapshot_url`` pointing to the frame that triggered the detection.

    Supervisors can mark alerts as reviewed via :meth:`mark_reviewed`.

    Implements **FR8** (detect suspicious activities), **FR9** (calculate
    cheating probability), **FR10** (generate alerts when threshold exceeded),
    and **FR12** (allow instructors to review alerts).
    """

    class BehaviorType(models.TextChoices):
        PHONE_DETECTED = 'PHONE_DETECTED', 'Phone Detected'
        MULTIPLE_FACES = 'MULTIPLE_FACES', 'Multiple Faces'
        LOOKING_AWAY = 'LOOKING_AWAY', 'Looking Away'
        OTHER_PERSON = 'OTHER_PERSON', 'Other Person Detected'
        OBJECT_DETECTED = 'OBJECT_DETECTED', 'Unauthorized Object Detected'

    class Severity(models.TextChoices):
        LOW = 'LOW', 'Low'
        MEDIUM = 'MEDIUM', 'Medium'
        HIGH = 'HIGH', 'High'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name='alerts')
    timestamp_sec = models.PositiveIntegerField(null=False, blank=False)
    behavior_type = models.CharField(max_length=50, choices=BehaviorType.choices, null=False, blank=False)
    severity = models.CharField(max_length=20, choices=Severity.choices, default=Severity.MEDIUM)
    confidence_score = models.FloatField(
        null=False, blank=False,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)]
    )
    metadata = models.JSONField(default=dict, blank=True)
    snapshot_url = models.URLField(
        max_length=500, blank=True,
        help_text='URL to the frame snapshot showing visual evidence.',
    )
    is_reviewed = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_alerts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['session', 'behavior_type']),
            models.Index(fields=['is_reviewed', 'created_at']),
            models.Index(fields=['severity', 'created_at']),
        ]

    def mark_reviewed(self, user):
        """Flag this alert as reviewed by *user* and persist the change.

        Sets ``is_reviewed = True``, records the reviewer and the current
        timestamp, then saves only the affected fields for efficiency.
        """
        self.is_reviewed = True
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.save(update_fields=['is_reviewed', 'reviewed_by', 'reviewed_at'])

    def __str__(self):
        return f"{self.behavior_type} at {self.timestamp_sec}s (confidence: {self.confidence_score})"


class AuditLog(models.Model):
    """Immutable log of security-relevant actions performed in the system.

    Records who did what, when, from which IP address, and with which
    browser.  Entries **cannot** be created, modified, or deleted through
    the admin interface — only programmatically by the backend.

    Implements **FR14** (maintain audit logs of all critical actions).
    """

    class ActionType(models.TextChoices):
        LOGIN = 'LOGIN', 'Login'
        LOGOUT = 'LOGOUT', 'Logout'
        VIDEO_UPLOADED = 'VIDEO_UPLOADED', 'Video Uploaded'
        VIDEO_DELETED = 'VIDEO_DELETED', 'Video Deleted'
        ANALYSIS_STARTED = 'ANALYSIS_STARTED', 'AI Analysis Started'
        ANALYSIS_COMPLETED = 'ANALYSIS_COMPLETED', 'AI Analysis Completed'
        SETTINGS_CHANGED = 'SETTINGS_CHANGED', 'Settings Changed'
        ALERT_REVIEWED = 'ALERT_REVIEWED', 'Alert Reviewed'
        REPORT_GENERATED = 'REPORT_GENERATED', 'Report Generated'
        USER_CREATED = 'USER_CREATED', 'User Created'
        USER_DELETED = 'USER_DELETED', 'User Deleted'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
    )
    action = models.CharField(max_length=50, choices=ActionType.choices, null=False, blank=False)
    target_resource = models.CharField(max_length=500, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    performed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-performed_at']
        indexes = [
            models.Index(fields=['action', 'performed_at']),
            models.Index(fields=['user', 'performed_at']),
        ]

    def __str__(self):
        return f"{self.action} by {self.user.email if self.user else 'Unknown'} at {self.performed_at}"


class SystemSettings(models.Model):
    """Admin-configurable key/value settings for the platform.

    Examples include the AI confidence threshold that triggers alerts and
    the video-retention period in days.  Each change records *who* made it
    via ``updated_by``.

    Implements **FR13** (allow administrators to configure system settings).
    """

    setting_key = models.CharField(max_length=100, primary_key=True)
    setting_value = models.CharField(max_length=500, null=False, blank=False)
    description = models.TextField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='updated_settings',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'system setting'
        verbose_name_plural = 'system settings'

    def __str__(self):
        return f"{self.setting_key} = {self.setting_value}"


class AnalysisJob(models.Model):
    """Tracks a single AI-analysis run against an exam session's video.

    Lifecycle: ``QUEUED → PROCESSING → COMPLETED`` (or ``FAILED``).

    The AI worker picks up queued jobs, sets ``started_at``, processes
    frames at the configured ``frame_sample_rate``, creates :class:`Alert`
    objects for each detection, and finally writes a :class:`Report`.

    Implements **FR5** (process uploaded videos offline), **FR6** (extract
    frames), and **FR7** (analyse behaviour using AI models).
    """

    class Status(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        PROCESSING = 'PROCESSING', 'Processing'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ExamSession, on_delete=models.CASCADE, related_name='analysis_job')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    ai_model_version = models.CharField(max_length=100, blank=True)
    frame_sample_rate = models.PositiveIntegerField(
        default=1, help_text='Analyze every Nth frame.',
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f"Analysis for {self.session} ({self.status})"


class Report(models.Model):
    """Aggregated analysis report for a completed exam session.

    Summarises the AI findings: overall cheating probability, total alert
    count, breakdown by behaviour type, and a human-readable summary.
    Generated automatically when an :class:`AnalysisJob` completes.

    Implements **FR11** (generate detailed analysis reports) and
    **FR12** (allow instructors to review reports).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ExamSession, on_delete=models.CASCADE, related_name='report')
    overall_cheating_probability = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    total_alerts = models.PositiveIntegerField(default=0)
    alerts_by_type = models.JSONField(default=dict, blank=True)
    processing_time_seconds = models.FloatField(null=True, blank=True)
    summary = models.TextField(blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['overall_cheating_probability']),
            models.Index(fields=['generated_at']),
        ]

    def __str__(self):
        return f"Report for {self.session} (prob: {self.overall_cheating_probability})"

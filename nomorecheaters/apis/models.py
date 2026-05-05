import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def video_upload_to(instance: "Video", filename: str) -> str:
    return f"exam-videos/{instance.session_id}/{filename}"


class User(AbstractUser):
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
        return self.exam.instructor

    def __str__(self):
        return f"{self.exam.name} - {self.student_identifier}"


class Video(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ExamSession, on_delete=models.CASCADE, related_name='video')
    file = models.FileField(upload_to=video_upload_to)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    file_hash = models.CharField(max_length=64, unique=True, null=False, blank=False)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['uploaded_at']),
            models.Index(fields=['file_hash']),
        ]

    def __str__(self):
        return f"Video for {self.session.exam.name}"


class Alert(models.Model):
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
        self.is_reviewed = True
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.save(update_fields=['is_reviewed', 'reviewed_by', 'reviewed_at'])

    def __str__(self):
        return f"{self.behavior_type} at {self.timestamp_sec}s (confidence: {self.confidence_score})"


class AuditLog(models.Model):
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

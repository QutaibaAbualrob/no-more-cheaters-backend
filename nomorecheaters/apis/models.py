from django.db import models

# Create your models here.
import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator


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


class ExamSession(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PROCESSING = 'PROCESSING', 'Processing'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='exam_sessions',
        limit_choices_to={'role__in': [User.Role.ADMIN, User.Role.INSTRUCTOR]}
    )
    student_identifier = models.CharField(max_length=255, null=False, blank=False)
    exam_name = models.CharField(max_length=255, null=False, blank=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.exam_name} - {self.student_identifier}"


class Video(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ExamSession, on_delete=models.CASCADE, related_name='video')
    file_url = models.URLField(max_length=500, null=True, blank=True)
    file_hash = models.CharField(max_length=64, unique=True, null=False, blank=False)
    duration_seconds = models.IntegerField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Video for {self.session.exam_name}"


class Alert(models.Model):
    class BehaviorType(models.TextChoices):
        PHONE_DETECTED = 'PHONE_DETECTED', 'Phone Detected'
        MULTIPLE_FACES = 'MULTIPLE_FACES', 'Multiple Faces'
        LOOKING_AWAY = 'LOOKING_AWAY', 'Looking Away'
        OTHER_PERSON = 'OTHER_PERSON', 'Other Person Detected'
        OBJECT_DETECTED = 'OBJECT_DETECTED', 'Unauthorized Object Detected'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name='alerts')
    timestamp_sec = models.IntegerField(null=False, blank=False)
    behavior_type = models.CharField(max_length=50, choices=BehaviorType.choices, null=False, blank=False)
    confidence_score = models.FloatField(
        null=False, blank=False,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)]
    )
    is_reviewed = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_alerts')
    created_at = models.DateTimeField(auto_now_add=True)
    
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
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=50, choices=ActionType.choices, null=False, blank=False)
    target_resource = models.CharField(max_length=500, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    performed_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-performed_at']
    
    def __str__(self):
        return f"{self.action} by {self.user.email if self.user else 'Unknown'} at {self.performed_at}"


class SystemSettings(models.Model):
    setting_key = models.CharField(max_length=100, primary_key=True)
    setting_value = models.CharField(max_length=500, null=False, blank=False)
    description = models.TextField(null=True, blank=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='updated_settings')
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.setting_key} = {self.setting_value}"
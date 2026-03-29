import uuid
from django.conf import settings
from django.db import models


class SystemSettings(models.Model):
    alert_threshold = models.FloatField(
        default=0.65,
        help_text="Cheating probability above this triggers alerts (0–1).",
    )
    retention_days = models.PositiveIntegerField(
        default=30,
        help_text="Raw video files older than this are deleted.",
    )

    class Meta:
        verbose_name_plural = "System settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def video_upload_to(instance, filename):
    return f"videos/{instance.sha256 or uuid.uuid4().hex}_{filename}"


class Video(models.Model):
    file = models.FileField(upload_to=video_upload_to)
    sha256 = models.CharField(max_length=64, db_index=True)
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=128, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="videos",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Video {self.pk} ({self.sha256[:12]}…)"


class AnalysisJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="jobs")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    error_message = models.TextField(blank=True)
    rq_job_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AnalysisReport(models.Model):
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="reports")
    job = models.OneToOneField(
        AnalysisJob, on_delete=models.CASCADE, related_name="report"
    )
    cheat_probability = models.FloatField(default=0.0)
    raw_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Alert(models.Model):
    report = models.ForeignKey(
        AnalysisReport, on_delete=models.CASCADE, related_name="alerts"
    )
    timestamp_seconds = models.FloatField(default=0.0)
    alert_type = models.CharField(max_length=64, db_index=True)
    confidence = models.FloatField(default=0.0)
    evidence = models.TextField(blank=True)

    class Meta:
        ordering = ["timestamp_seconds"]

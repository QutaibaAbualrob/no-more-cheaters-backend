from __future__ import annotations

import hashlib
from pathlib import Path

from django.conf import settings
from django.db import models


def _video_upload_to(instance: "Video", filename: str) -> str:
    safe = Path(filename).name
    return f"videos/{instance.uploaded_by_id}/{safe}"


class Video(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="videos"
    )
    file = models.FileField(upload_to=_video_upload_to)
    sha256 = models.CharField(max_length=64, db_index=True)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.status})"

    @staticmethod
    def sha256_for_upload(uploaded) -> str:
        h = hashlib.sha256()
        for chunk in uploaded.chunks():
            h.update(chunk)
        return h.hexdigest()


class AnalysisResult(models.Model):
    video = models.OneToOneField(Video, on_delete=models.CASCADE, related_name="analysis")
    summary = models.TextField(blank=True)
    cheating_students = models.JSONField(default=list, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"AnalysisResult(video_id={self.video_id})"


class GlobalThresholdSettings(models.Model):
    gaze_threshold = models.FloatField(default=0.75)
    noise_threshold = models.FloatField(default=0.65)
    multiple_faces_threshold = models.FloatField(default=0.5)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls) -> "GlobalThresholdSettings":
        obj, _created = cls.objects.get_or_create(id=1)
        return obj

    def save(self, *args, **kwargs):
        self.id = 1
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return "GlobalThresholdSettings(1)"


class UserThresholdSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="thresholds")
    gaze_threshold = models.FloatField(default=0.75)
    noise_threshold = models.FloatField(default=0.65)
    multiple_faces_threshold = models.FloatField(default=0.5)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"UserThresholdSettings(user_id={self.user_id})"


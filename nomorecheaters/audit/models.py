from __future__ import annotations

from django.conf import settings
from django.db import models


class SystemLog(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="system_logs",
    )
    level = models.CharField(max_length=16, choices=Level.choices, default=Level.INFO)
    message = models.CharField(max_length=500)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


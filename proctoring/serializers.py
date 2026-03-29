from django.conf import settings
from rest_framework import serializers

from .models import Alert, AnalysisJob, AnalysisReport, SystemSettings, Video


class VideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Video
        fields = (
            "id",
            "file",
            "sha256",
            "original_filename",
            "content_type",
            "size_bytes",
            "uploaded_by",
            "created_at",
        )
        read_only_fields = (
            "id",
            "sha256",
            "original_filename",
            "content_type",
            "size_bytes",
            "uploaded_by",
            "created_at",
        )


class VideoCreateSerializer(serializers.Serializer):
    file = serializers.FileField()

    ALLOWED = {".mp4", ".webm", ".mov", ".mkv", ".avi"}

    def validate_file(self, file):
        from pathlib import Path

        name = Path(file.name or "").suffix.lower()
        if name not in self.ALLOWED:
            raise serializers.ValidationError(
                f"Unsupported format. Allowed: {sorted(self.ALLOWED)}"
            )
        if file.size > settings.MAX_UPLOAD_BYTES:
            raise serializers.ValidationError("File too large.")
        return file


class AnalysisJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisJob
        fields = (
            "id",
            "video",
            "status",
            "error_message",
            "rq_job_id",
            "created_at",
            "started_at",
            "completed_at",
        )
        read_only_fields = fields


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = (
            "id",
            "timestamp_seconds",
            "alert_type",
            "confidence",
            "evidence",
        )


class AnalysisReportSerializer(serializers.ModelSerializer):
    alerts = AlertSerializer(many=True, read_only=True)

    class Meta:
        model = AnalysisReport
        fields = (
            "id",
            "video",
            "job",
            "cheat_probability",
            "raw_json",
            "created_at",
            "alerts",
        )
        read_only_fields = fields


class SystemSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSettings
        fields = ("id", "alert_threshold", "retention_days")
        read_only_fields = ("id",)

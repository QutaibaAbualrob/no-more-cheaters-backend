from __future__ import annotations

from rest_framework import serializers

from .models import AnalysisResult, GlobalThresholdSettings, UserThresholdSettings, Video


class AnalysisResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisResult
        fields = ("id", "summary", "cheating_students", "details", "created_at", "updated_at")
        read_only_fields = fields


class VideoSerializer(serializers.ModelSerializer):
    analysis = AnalysisResultSerializer(read_only=True)
    uploaded_by_email = serializers.CharField(source="uploaded_by.email", read_only=True)

    class Meta:
        model = Video
        fields = (
            "id",
            "original_filename",
            "content_type",
            "size_bytes",
            "status",
            "sha256",
            "created_at",
            "updated_at",
            "uploaded_by_email",
            "analysis",
        )
        read_only_fields = fields


class VideoUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        max_bytes = 524_288_000  # 500MB default; can be overridden at reverse proxy
        if getattr(value, "size", 0) and value.size > max_bytes:
            raise serializers.ValidationError("File is too large.")
        return value


class GlobalThresholdSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalThresholdSettings
        fields = ("gaze_threshold", "noise_threshold", "multiple_faces_threshold", "updated_at")
        read_only_fields = ("updated_at",)


class UserThresholdSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserThresholdSettings
        fields = ("gaze_threshold", "noise_threshold", "multiple_faces_threshold", "updated_at")
        read_only_fields = ("updated_at",)


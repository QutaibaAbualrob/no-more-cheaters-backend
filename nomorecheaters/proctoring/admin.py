from django.contrib import admin

from .models import AnalysisResult, GlobalThresholdSettings, UserThresholdSettings, Video


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "uploaded_by", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("original_filename", "uploaded_by__email", "sha256")


@admin.register(AnalysisResult)
class AnalysisResultAdmin(admin.ModelAdmin):
    list_display = ("id", "video", "created_at", "updated_at")
    search_fields = ("video__original_filename", "video__uploaded_by__email")


@admin.register(GlobalThresholdSettings)
class GlobalThresholdSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "gaze_threshold", "noise_threshold", "multiple_faces_threshold", "updated_at")


@admin.register(UserThresholdSettings)
class UserThresholdSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "gaze_threshold", "noise_threshold", "multiple_faces_threshold", "updated_at")


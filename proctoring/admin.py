from django.contrib import admin
from .models import Alert, AnalysisJob, AnalysisReport, SystemSettings, Video


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("id", "sha256", "uploaded_by", "size_bytes", "created_at")
    search_fields = ("sha256", "original_filename")


@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    list_display = ("id", "video", "status", "created_at")


@admin.register(AnalysisReport)
class AnalysisReportAdmin(admin.ModelAdmin):
    list_display = ("id", "video", "cheat_probability", "created_at")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("id", "report", "alert_type", "confidence", "timestamp_seconds")


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not SystemSettings.objects.exists()

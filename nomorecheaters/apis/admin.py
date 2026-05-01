from django.contrib import admin

# Register your models here.
from .models import User, ExamSession, Video, Alert, AuditLog, SystemSettings


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ['email', 'username', 'role', 'is_active', 'created_at']
    list_filter = ['role', 'is_active']
    search_fields = ['email', 'username']


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = ['exam_name', 'student_identifier', 'instructor', 'status', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['exam_name', 'student_identifier', 'instructor__email']


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ['session', 'file_hash', 'duration_seconds', 'uploaded_at']
    search_fields = ['session__exam_name', 'file_hash']
    readonly_fields = ['file_hash', 'uploaded_at']


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ['session', 'behavior_type', 'timestamp_sec', 'confidence_score', 'is_reviewed']
    list_filter = ['behavior_type', 'is_reviewed', 'created_at']
    search_fields = ['session__exam_name', 'session__student_identifier']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['action', 'user', 'target_resource', 'performed_at']
    list_filter = ['action', 'performed_at']
    search_fields = ['user__email', 'target_resource']
    readonly_fields = ['performed_at']


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ['setting_key', 'setting_value', 'updated_by', 'updated_at']
    search_fields = ['setting_key', 'description']
    readonly_fields = ['updated_at']

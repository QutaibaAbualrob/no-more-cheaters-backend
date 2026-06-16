from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Alert, AnalysisJob, AuditLog, AutoExamSession, Exam, ExamSession,
    Notification, Report, Student, SystemSettings, User, UserPreferences,
    Video, Workspace, WorkspaceInvite, WorkspaceMembership,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ['email', 'username', 'role', 'is_active', 'is_staff', 'created_at']
    list_filter = ['role', 'is_active', 'is_staff', 'is_superuser']
    search_fields = ['email', 'username']
    ordering = ['email']
    readonly_fields = ['id', 'created_at', 'date_joined', 'last_login']

    fieldsets = DjangoUserAdmin.fieldsets + (
        ('No More Cheaters profile', {'fields': ('id', 'role', 'created_at')}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('No More Cheaters profile', {'fields': ('email', 'role')}),
    )


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'student_id', 'owner', 'faculty', 'major', 'academic_year', 'created_at']
    list_filter = ['faculty', 'academic_year', 'created_at']
    search_fields = ['full_name', 'student_id', 'owner__email', 'major']
    autocomplete_fields = ['owner']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_select_related = ['owner']
    date_hierarchy = 'created_at'


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ['name', 'instructor', 'created_at', 'updated_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['name', 'description', 'instructor__email', 'instructor__username']
    autocomplete_fields = ['instructor']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_select_related = ['instructor']
    date_hierarchy = 'created_at'


@admin.register(UserPreferences)
class UserPreferencesAdmin(admin.ModelAdmin):
    list_display = ['user', 'preferred_language', 'timezone', 'theme', 'email_notifications', 'dashboard_alerts', 'updated_at']
    list_filter = ['theme', 'email_notifications', 'dashboard_alerts', 'updated_at']
    search_fields = ['user__email', 'user__username', 'preferred_language', 'timezone']
    autocomplete_fields = ['user']
    readonly_fields = ['updated_at']
    list_select_related = ['user']


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = ['exam', 'student_identifier', 'status', 'created_at', 'updated_at']
    list_filter = ['status', 'created_at', 'updated_at']
    search_fields = ['exam__name', 'student_identifier', 'exam__instructor__email']
    autocomplete_fields = ['exam']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_select_related = ['exam', 'exam__instructor']
    date_hierarchy = 'created_at'


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ['session', 'original_filename', 'content_type', 'size_bytes', 'duration_seconds', 'uploaded_at']
    list_filter = ['content_type', 'uploaded_at']
    search_fields = ['session__exam__name', 'session__student_identifier', 'original_filename', 'file_hash']
    autocomplete_fields = ['session']
    readonly_fields = ['id', 'file_hash', 'uploaded_at']
    list_select_related = ['session', 'session__exam']
    date_hierarchy = 'uploaded_at'


@admin.action(description='Mark selected alerts as reviewed')
def mark_alerts_reviewed(modeladmin, request, queryset):
    for alert in queryset.select_related('reviewed_by'):
        alert.mark_reviewed(request.user)


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = [
        'session',
        'behavior_type',
        'severity',
        'timestamp_sec',
        'confidence_score',
        'is_reviewed',
        'created_at',
    ]
    list_filter = ['behavior_type', 'severity', 'is_reviewed', 'created_at']
    search_fields = ['session__exam__name', 'session__student_identifier']
    autocomplete_fields = ['session', 'reviewed_by']
    readonly_fields = ['id', 'created_at', 'reviewed_at']
    list_select_related = ['session', 'session__exam', 'reviewed_by']
    date_hierarchy = 'created_at'
    actions = [mark_alerts_reviewed]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['action', 'user', 'target_resource', 'ip_address', 'performed_at']
    list_filter = ['action', 'performed_at']
    search_fields = ['user__email', 'target_resource', 'ip_address']
    autocomplete_fields = ['user']
    readonly_fields = ['id', 'action', 'user', 'target_resource', 'ip_address', 'user_agent', 'metadata', 'performed_at']
    list_select_related = ['user']
    date_hierarchy = 'performed_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ['setting_key', 'setting_value', 'updated_by', 'updated_at']
    search_fields = ['setting_key', 'description']
    autocomplete_fields = ['updated_by']
    readonly_fields = ['updated_at']
    list_select_related = ['updated_by']


@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    list_display = ['session', 'status', 'ai_model_version', 'started_at', 'completed_at', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['session__exam__name', 'session__student_identifier', 'ai_model_version']
    autocomplete_fields = ['session']
    readonly_fields = ['id', 'created_at']
    list_select_related = ['session', 'session__exam']
    date_hierarchy = 'created_at'


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ['session', 'overall_cheating_probability', 'total_alerts', 'processing_time_seconds', 'generated_at']
    list_filter = ['generated_at']
    search_fields = ['session__exam__name', 'session__student_identifier', 'summary']
    autocomplete_fields = ['session']
    readonly_fields = ['id', 'generated_at']
    list_select_related = ['session', 'session__exam']
    date_hierarchy = 'generated_at'


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['notif_type', 'recipient', 'title', 'is_read', 'is_dismissed', 'created_at']
    list_filter = ['notif_type', 'is_read', 'is_dismissed', 'created_at']
    search_fields = ['recipient__email', 'recipient__username', 'title', 'body']
    autocomplete_fields = ['recipient']
    readonly_fields = ['id', 'created_at']
    list_select_related = ['recipient']
    date_hierarchy = 'created_at'


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'created_at']
    list_filter = ['created_at']
    search_fields = ['name', 'owner__email', 'owner__username']
    autocomplete_fields = ['owner']
    readonly_fields = ['id', 'created_at']
    list_select_related = ['owner']
    date_hierarchy = 'created_at'


@admin.register(WorkspaceMembership)
class WorkspaceMembershipAdmin(admin.ModelAdmin):
    list_display = ['workspace', 'instructor', 'joined_at']
    list_filter = ['joined_at']
    search_fields = ['workspace__name', 'instructor__email', 'instructor__username']
    autocomplete_fields = ['workspace', 'instructor']
    readonly_fields = ['id', 'joined_at']
    list_select_related = ['workspace', 'instructor']
    date_hierarchy = 'joined_at'


@admin.register(WorkspaceInvite)
class WorkspaceInviteAdmin(admin.ModelAdmin):
    list_display = ['instructor', 'dean', 'target_name', 'status', 'created_at', 'responded_at']
    list_filter = ['status', 'created_at', 'responded_at']
    search_fields = [
        'instructor__email', 'dean__email', 'workspace__name', 'exam__name',
    ]
    autocomplete_fields = ['dean', 'instructor', 'workspace', 'exam']
    # token is a secret used by the public accept/decline links — never editable.
    readonly_fields = ['id', 'token', 'created_at', 'responded_at']
    list_select_related = ['dean', 'instructor', 'workspace', 'exam']
    date_hierarchy = 'created_at'


@admin.register(AutoExamSession)
class AutoExamSessionAdmin(admin.ModelAdmin):
    list_display = ['exam', 'instructor', 'scheduled_start', 'scheduled_end', 'is_auto', 'created_at']
    list_filter = ['is_auto', 'scheduled_start', 'created_at']
    search_fields = ['exam__name', 'instructor__email', 'instructor__username']
    autocomplete_fields = ['exam', 'instructor']
    readonly_fields = ['id', 'created_at']
    list_select_related = ['exam', 'instructor']
    date_hierarchy = 'scheduled_start'

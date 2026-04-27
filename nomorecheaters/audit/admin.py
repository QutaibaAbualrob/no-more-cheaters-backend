from django.contrib import admin

from .models import SystemLog


@admin.register(SystemLog)
class SystemLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "level", "actor", "message")
    list_filter = ("level", "created_at")
    search_fields = ("message", "actor__email")


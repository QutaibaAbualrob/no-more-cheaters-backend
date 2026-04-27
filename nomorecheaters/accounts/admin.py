from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import PasswordResetCode, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "name", "institution", "role", "is_active", "is_staff")
    search_fields = ("email", "name", "institution")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("name", "institution", "role")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "name", "institution", "role", "password1", "password2"),
            },
        ),
    )

    readonly_fields = ("date_joined", "last_login")


@admin.register(PasswordResetCode)
class PasswordResetCodeAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at", "expires_at", "used_at", "attempts")
    list_filter = ("used_at", "created_at")
    search_fields = ("email",)
    readonly_fields = ("created_at", "expires_at", "used_at", "attempts", "code_hash", "email", "user")

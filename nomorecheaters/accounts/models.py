from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models
from django.utils import timezone

from .managers import UserManager


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        USER = "user", "User"

    username = None  # type: ignore[assignment]

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150)
    institution = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.USER)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    def __str__(self) -> str:
        return f"{self.email} ({self.role})"


class PasswordResetCode(models.Model):
    """
    Short-lived verification code used for password reset.

    Notes:
    - Store only a hash of the code.
    - Codes expire quickly (90 seconds as required).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="password_reset_codes",
    )
    email = models.EmailField(db_index=True)
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["email", "expires_at"]),
        ]

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

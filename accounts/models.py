from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        INSTRUCTOR = "instructor", "Instructor"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INSTRUCTOR)

    def is_admin_role(self) -> bool:
        return self.role == self.Role.ADMIN

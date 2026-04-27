from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    message = "Admin privileges are required."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        return getattr(user, "role", "") == "admin" or bool(getattr(user, "is_superuser", False))


from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if getattr(u, "is_superuser", False):
            return True
        return getattr(u, "is_admin_role", lambda: False)()


class IsInstructorOrAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if getattr(u, "is_superuser", False):
            return True
        return getattr(u, "role", None) in ("instructor", "admin")

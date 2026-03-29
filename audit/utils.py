from .models import AuditLog


def get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_audit(user, action: str, resource: str, resource_id: str = "", request=None, details=None):
    AuditLog.objects.create(
        user=user if user and user.is_authenticated else None,
        action=action,
        resource=resource,
        resource_id=str(resource_id)[:64],
        details=details or {},
        ip_address=get_client_ip(request) if request else None,
    )

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from .models import SystemLog


def log_event(
    *,
    actor,
    level: str,
    message: str,
    request: HttpRequest | None,
    meta: dict[str, Any] | None = None,
):
    data: dict[str, Any] = dict(meta or {})

    if request is not None:
        data.setdefault("ip", request.META.get("REMOTE_ADDR", ""))
        data.setdefault("ua", request.META.get("HTTP_USER_AGENT", ""))
        data.setdefault("path", request.path)
        data.setdefault("method", request.method)

    SystemLog.objects.create(actor=actor if getattr(actor, "is_authenticated", False) else None, level=level, message=message, meta=data)


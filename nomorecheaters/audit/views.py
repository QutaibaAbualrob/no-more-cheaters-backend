from __future__ import annotations

import platform
import time

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole
from proctoring.models import AnalysisResult, Video

from .models import SystemLog
from .serializers import SystemLogSerializer

APP_STARTED_AT = time.monotonic()

User = get_user_model()


class SystemLogListView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        qs = SystemLog.objects.all()
        page = int(request.query_params.get("page", "1") or "1")
        page_size = int(request.query_params.get("page_size", "50") or "50")

        page = max(1, page)
        page_size = min(max(1, page_size), 200)

        start = (page - 1) * page_size
        end = start + page_size
        items = qs[start:end]

        return Response(
            {
                "count": qs.count(),
                "page": page,
                "page_size": page_size,
                "results": SystemLogSerializer(items, many=True).data,
            }
        )


class SystemMetricsView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        now = timezone.now()
        uptime_seconds = int(time.monotonic() - APP_STARTED_AT)

        return Response(
            {
                "timestamp": now.isoformat(),
                "uptime_seconds": uptime_seconds,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "counts": {
                    "users": User.objects.count(),
                    "videos": Video.objects.count(),
                    "analyses": AnalysisResult.objects.count(),
                },
            }
        )


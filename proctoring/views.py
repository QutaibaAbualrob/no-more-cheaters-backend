import hashlib
from pathlib import Path

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
#from django_rq import get_queue

from accounts.permissions import IsAdminRole, IsInstructorOrAdmin
from audit.utils import log_audit
from .models import AnalysisJob, AnalysisReport, SystemSettings, Video
from .serializers import (
    AnalysisJobSerializer,
    AnalysisReportSerializer,
    SystemSettingsSerializer,
    VideoCreateSerializer,
    VideoSerializer,
)
from .tasks import run_analysis_job


def _sha256_file_upload(uploaded) -> str:
    h = hashlib.sha256()
    for chunk in uploaded.chunks():
        h.update(chunk)
    return h.hexdigest()


class VideoViewSet(viewsets.ModelViewSet):
    permission_classes = [IsInstructorOrAdmin]
    queryset = Video.objects.all()
    serializer_class = VideoSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        u = self.request.user
        if getattr(u, "role", None) == "admin":
            return qs
        return qs.filter(uploaded_by=u)

    def create(self, request, *args, **kwargs):
        ser = VideoCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        uploaded = ser.validated_data["file"]
        digest = _sha256_file_upload(uploaded)
        uploaded.seek(0)

        existing = Video.objects.filter(sha256=digest).first()
        if existing:
            if existing.uploaded_by_id != request.user.id and getattr(
                request.user, "role", None
            ) != "admin":
                return Response(
                    {"detail": "Video already exists (owned by another user)."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            log_audit(request.user, "upload_duplicate", "video", str(existing.pk), request)
            out = VideoSerializer(existing, context={"request": request})
            return Response(
                {"duplicate": True, "video": out.data},
                status=status.HTTP_200_OK,
            )

        video = Video(
            sha256=digest,
            original_filename=getattr(uploaded, "name", "") or "",
            content_type=getattr(uploaded, "content_type", "") or "",
            size_bytes=getattr(uploaded, "size", 0) or 0,
            uploaded_by=request.user,
        )
        video.file.save(Path(uploaded.name).name, uploaded, save=False)
        video.save()
        log_audit(request.user, "upload", "video", str(video.pk), request)
        out = VideoSerializer(video, context={"request": request})
        return Response(out.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="analyze")
    def analyze(self, request, pk=None):
        video = self.get_object()
        job = AnalysisJob.objects.create(video=video, status=AnalysisJob.Status.QUEUED)
      #  queue = get_queue("default")
        #django_job = queue.enqueue(run_analysis_job, job.id, request.user.id)
        job.rq_job_id = str(getattr(django_job, "id", django_job))
        job.save(update_fields=["rq_job_id"])
        log_audit(
            request.user,
            "analysis_enqueue",
            "video",
            str(video.pk),
            request,
            details={"job_id": job.id},
        )
        return Response(
            AnalysisJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ReportViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsInstructorOrAdmin]
    serializer_class = AnalysisReportSerializer
    queryset = AnalysisReport.objects.select_related("video", "job").prefetch_related(
        "alerts"
    )

    def get_queryset(self):
        qs = super().get_queryset()
        u = self.request.user
        if getattr(u, "role", None) == "admin":
            return qs
        return qs.filter(video__uploaded_by=u)


class SystemSettingsView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        s = SystemSettings.get()
        return Response(SystemSettingsSerializer(s).data)

    def patch(self, request):
        s = SystemSettings.get()
        ser = SystemSettingsSerializer(s, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(
            request.user,
            "settings_update",
            "systemsettings",
            "1",
            request,
        )
        return Response(ser.data)


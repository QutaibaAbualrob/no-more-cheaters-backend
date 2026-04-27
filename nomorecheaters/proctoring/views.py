from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole
from audit.utils import log_event

from .models import AnalysisResult, GlobalThresholdSettings, UserThresholdSettings, Video
from .serializers import (
    AnalysisResultSerializer,
    GlobalThresholdSettingsSerializer,
    UserThresholdSettingsSerializer,
    VideoSerializer,
    VideoUploadSerializer,
)
from .services import generate_fake_analysis


class VideoViewSet(viewsets.ViewSet):
    """
    Video upload + processing endpoints.
    """

    def list(self, request):
        qs = Video.objects.select_related("uploaded_by").prefetch_related("analysis")
        if getattr(request.user, "role", "") != "admin":
            qs = qs.filter(uploaded_by=request.user)
        return Response(VideoSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        qs = Video.objects.select_related("uploaded_by").prefetch_related("analysis")
        video = qs.filter(pk=pk).first()
        if not video:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if getattr(request.user, "role", "") != "admin" and video.uploaded_by_id != request.user.id:
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        return Response(VideoSerializer(video).data)

    def create(self, request):
        serializer = VideoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded = serializer.validated_data["file"]

        digest = Video.sha256_for_upload(uploaded)
        uploaded.seek(0)

        video = Video(
            uploaded_by=request.user,
            file=uploaded,
            sha256=digest,
            original_filename=getattr(uploaded, "name", "") or "upload",
            content_type=getattr(uploaded, "content_type", "") or "",
            size_bytes=getattr(uploaded, "size", 0) or 0,
            status=Video.Status.UPLOADED,
        )
        video.save()
        log_event(actor=request.user, level="info", message=f"Video uploaded: {video.original_filename}", request=request)
        return Response(VideoSerializer(video).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="analyze")
    def analyze(self, request, pk=None):
        video = Video.objects.select_related("uploaded_by").filter(pk=pk).first()
        if not video:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if getattr(request.user, "role", "") != "admin" and video.uploaded_by_id != request.user.id:
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            video.status = Video.Status.PROCESSING
            video.save(update_fields=["status", "updated_at"])

            summary, cheating_students, details = generate_fake_analysis(video=video)
            result, _created = AnalysisResult.objects.update_or_create(
                video=video,
                defaults={"summary": summary, "cheating_students": cheating_students, "details": details},
            )

            video.status = Video.Status.COMPLETED
            video.save(update_fields=["status", "updated_at"])

        log_event(actor=request.user, level="info", message=f"Video analyzed: {video.original_filename}", request=request)
        return Response({"video": VideoSerializer(video).data, "analysis": AnalysisResultSerializer(result).data})


class HistoryView(APIView):
    def get(self, request):
        qs = Video.objects.select_related("uploaded_by", "analysis").filter(status=Video.Status.COMPLETED)
        if getattr(request.user, "role", "") != "admin":
            qs = qs.filter(uploaded_by=request.user)
        return Response(VideoSerializer(qs, many=True).data)


class ThresholdsView(APIView):
    def get(self, request):
        global_settings = GlobalThresholdSettings.get_solo()
        user_settings = UserThresholdSettings.objects.filter(user=request.user).first()

        return Response(
            {
                "global": GlobalThresholdSettingsSerializer(global_settings).data,
                "user": UserThresholdSettingsSerializer(user_settings).data if user_settings else None,
                "effective": UserThresholdSettingsSerializer(user_settings).data
                if user_settings
                else GlobalThresholdSettingsSerializer(global_settings).data,
            }
        )

    def put(self, request):
        settings_obj, _created = UserThresholdSettings.objects.get_or_create(user=request.user)
        serializer = UserThresholdSettingsSerializer(settings_obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_event(actor=request.user, level="info", message="Updated personal AI thresholds.", request=request)
        return Response(serializer.data)


class GlobalThresholdsView(APIView):
    permission_classes = [IsAdminRole]

    def put(self, request):
        settings_obj = GlobalThresholdSettings.get_solo()
        serializer = GlobalThresholdSettingsSerializer(settings_obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_event(actor=request.user, level="warning", message="Updated global AI thresholds.", request=request)
        return Response(serializer.data)


class DashboardStatsView(APIView):
    def get(self, request):
        is_admin = getattr(request.user, "role", "") == "admin"

        videos = Video.objects.all()
        analyses = AnalysisResult.objects.all()

        if not is_admin:
            videos = videos.filter(uploaded_by=request.user)
            analyses = analyses.filter(video__uploaded_by=request.user)

        total_videos = videos.count()
        processing_videos = videos.filter(status=Video.Status.PROCESSING).count()
        completed_videos = videos.filter(status=Video.Status.COMPLETED).count()
        total_analyses = analyses.count()
        cheating_reports = analyses.exclude(cheating_students=[]).count()

        payload = {
            "videos_total": total_videos,
            "videos_processing": processing_videos,
            "videos_completed": completed_videos,
            "analyses_total": total_analyses,
            "cheating_reports_total": cheating_reports,
        }

        if is_admin:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            payload["users_total"] = User.objects.count()

        return Response(payload)


class DashboardActivityView(APIView):
    def get(self, request):
        is_admin = getattr(request.user, "role", "") == "admin"
        days = 14
        end = timezone.localdate()
        start = end - timedelta(days=days - 1)

        date_list = [start + timedelta(days=idx) for idx in range(days)]
        date_keys = [d.isoformat() for d in date_list]

        videos = Video.objects.all()
        analyses = AnalysisResult.objects.all()
        if not is_admin:
            videos = videos.filter(uploaded_by=request.user)
            analyses = analyses.filter(video__uploaded_by=request.user)

        video_counts = {
            row["day"]: row["count"]
            for row in videos.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id"))
        }
        analysis_counts = {
            row["day"]: row["count"]
            for row in analyses.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id"))
        }

        return Response(
            {
                "days": date_keys,
                "videos": [int(video_counts.get(d, 0)) for d in date_list],
                "analyses": [int(analysis_counts.get(d, 0)) for d in date_list],
            }
        )

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import parsers, status, viewsets
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


def _is_admin(user) -> bool:
    return getattr(user, "role", "") == "admin" or bool(getattr(user, "is_superuser", False))


def _create_video_from_upload(*, request, uploaded) -> Video:
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
    return video


def _analysis_alert_count(analysis: AnalysisResult) -> int:
    details = analysis.details or {}
    students_flagged = details.get("students_flagged")
    if isinstance(students_flagged, int):
        return students_flagged
    return len(analysis.cheating_students or [])


def _build_dashboard_analytics(*, request) -> dict:
    is_admin = _is_admin(request.user)

    videos = Video.objects.all()
    analyses = AnalysisResult.objects.select_related("video")

    if not is_admin:
        videos = videos.filter(uploaded_by=request.user)
        analyses = analyses.filter(video__uploaded_by=request.user)

    total_exams = videos.count()
    status_counts = {
        status_value: videos.filter(status=status_value).count()
        for status_value, _label in Video.Status.choices
    }

    analyses_list = list(analyses)
    total_alerts = sum(_analysis_alert_count(item) for item in analyses_list)
    flagged_exams = sum(1 for item in analyses_list if _analysis_alert_count(item) > 0)
    suspicion_scores = [
        item.details.get("suspicion_score")
        for item in analyses_list
        if isinstance(item.details, dict) and isinstance(item.details.get("suspicion_score"), (int, float))
    ]

    analytics = {
        "total_exams": total_exams,
        "total_videos": total_exams,
        "total_alerts": total_alerts,
        "total_analyses": len(analyses_list),
        "flagged_exams": flagged_exams,
        "clean_exams": max(len(analyses_list) - flagged_exams, 0),
        "pending_exams": status_counts.get(Video.Status.UPLOADED, 0),
        "processing_exams": status_counts.get(Video.Status.PROCESSING, 0),
        "completed_exams": status_counts.get(Video.Status.COMPLETED, 0),
        "failed_exams": status_counts.get(Video.Status.FAILED, 0),
        "status_counts": status_counts,
        "average_alerts_per_exam": round(total_alerts / total_exams, 2) if total_exams else 0,
        "average_suspicion_score": round(sum(suspicion_scores) / len(suspicion_scores), 3) if suspicion_scores else 0,
        "cheating_reports_total": flagged_exams,
    }

    if is_admin:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        analytics["total_users"] = User.objects.count()

    return analytics


class VideoViewSet(viewsets.ViewSet):
    """
    Video upload + processing endpoints.
    """

    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def list(self, request):
        qs = Video.objects.select_related("uploaded_by").prefetch_related("analysis")
        if not _is_admin(request.user):
            qs = qs.filter(uploaded_by=request.user)
        return Response(VideoSerializer(qs, many=True, context={"request": request}).data)

    def retrieve(self, request, pk=None):
        qs = Video.objects.select_related("uploaded_by").prefetch_related("analysis")
        video = qs.filter(pk=pk).first()
        if not video:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _is_admin(request.user) and video.uploaded_by_id != request.user.id:
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        return Response(VideoSerializer(video, context={"request": request}).data)

    def create(self, request):
        serializer = VideoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded = serializer.validated_data["file"]

        video = _create_video_from_upload(request=request, uploaded=uploaded)
        return Response(VideoSerializer(video, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="analyze")
    def analyze(self, request, pk=None):
        video = Video.objects.select_related("uploaded_by").filter(pk=pk).first()
        if not video:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _is_admin(request.user) and video.uploaded_by_id != request.user.id:
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
        return Response(
            {
                "video": VideoSerializer(video, context={"request": request}).data,
                "analysis": AnalysisResultSerializer(result).data,
            }
        )


class InstructorVideoUploadView(APIView):
    """
    Dedicated multipart endpoint for instructors to upload exam videos.
    """

    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        serializer = VideoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        video = _create_video_from_upload(request=request, uploaded=serializer.validated_data["file"])
        return Response(VideoSerializer(video, context={"request": request}).data, status=status.HTTP_201_CREATED)


class HistoryView(APIView):
    def get(self, request):
        qs = Video.objects.select_related("uploaded_by", "analysis").filter(status=Video.Status.COMPLETED)
        if not _is_admin(request.user):
            qs = qs.filter(uploaded_by=request.user)
        return Response(VideoSerializer(qs, many=True, context={"request": request}).data)


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
        analytics = _build_dashboard_analytics(request=request)
        return Response(
            {
                "videos_total": analytics["total_videos"],
                "videos_processing": analytics["processing_exams"],
                "videos_completed": analytics["completed_exams"],
                "analyses_total": analytics["total_analyses"],
                "cheating_reports_total": analytics["cheating_reports_total"],
                "total_exams": analytics["total_exams"],
                "total_alerts": analytics["total_alerts"],
                "flagged_exams": analytics["flagged_exams"],
                "clean_exams": analytics["clean_exams"],
                "pending_exams": analytics["pending_exams"],
                "failed_exams": analytics["failed_exams"],
                "average_alerts_per_exam": analytics["average_alerts_per_exam"],
                "average_suspicion_score": analytics["average_suspicion_score"],
                **({"users_total": analytics["total_users"]} if "total_users" in analytics else {}),
            }
        )


class DashboardAnalyticsView(APIView):
    def get(self, request):
        return Response(_build_dashboard_analytics(request=request))


class DashboardActivityView(APIView):
    def get(self, request):
        is_admin = _is_admin(request.user)
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

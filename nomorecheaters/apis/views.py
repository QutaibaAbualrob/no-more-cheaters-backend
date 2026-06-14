from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AnalysisJob, AuditLog, Exam, ExamSession, UserPreferences
from .selectors import (
    is_admin, is_dean, owned_students, owned_videos, recent_day_window, users_visible_to,
)
from .serializers import (
    ReportReadSerializer,
    StudentSerializer,
    UserPreferencesReadSerializer,
    UserPreferencesUpdateSerializer,
    UserReadSerializer,
    UserUpdateSerializer,
    VideoReadSerializer,
    VideoUploadSerializer,
)
from .services import (
    activity_series_for,
    build_thresholds_response,
    dashboard_stats_for,
    enqueue_analysis,
    get_available_upload_session,
    system_metrics,
    update_global_thresholds,
    update_user_thresholds,
    write_audit_log,
)


User = get_user_model()


class UsersListView(generics.ListAPIView):
    """List visible users.

    Admins can see all users. Instructors only receive their own user record,
    which keeps the endpoint safe while preserving the frontend's need to load
    the authenticated account details.
    """

    serializer_class = UserReadSerializer

    def get_queryset(self):
        return users_visible_to(self.request.user)


class DeleteUserView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve a user, with admin-only mutation support."""

    def get_queryset(self):
        return users_visible_to(self.request.user)

    def get_serializer_class(self):
        if self.request.method in {'PUT', 'PATCH'}:
            return UserUpdateSerializer
        return UserReadSerializer

    def perform_update(self, serializer):
        if not is_admin(self.request.user):
            raise PermissionDenied('Only administrators can update users.')
        serializer.save()

    def perform_destroy(self, instance):
        if not is_admin(self.request.user):
            raise PermissionDenied('Only administrators can delete users.')
        write_audit_log(
            self.request,
            AuditLog.ActionType.USER_DELETED,
            target_resource=str(instance.id),
            metadata={'email': instance.email},
        )
        instance.delete()


class StudentListCreateView(generics.ListCreateAPIView):
    """List the current user's roster students, or add a new one.

    The serializer reads ``owner`` from the request, so creates are always
    scoped to the authenticated instructor.
    """

    serializer_class = StudentSerializer

    def get_queryset(self):
        return owned_students(self.request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), 'request': self.request}


class StudentDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a single roster student (owner/admin only)."""

    serializer_class = StudentSerializer

    def get_queryset(self):
        return owned_students(self.request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), 'request': self.request}


class UserActivityView(APIView):
    """Return a seven-day activity series for a specific instructor/admin."""

    def get(self, request, pk):
        target_user = get_object_or_404(User, pk=pk)
        if not is_admin(request.user) and request.user.id != target_user.id:
            raise PermissionDenied('You can only view your own activity.')

        days, labels = recent_day_window()
        sessions = target_user.exams.values_list('sessions__id', flat=True)
        videos = owned_videos(target_user)
        analyses = AnalysisJob.objects.filter(session_id__in=sessions)
        return Response({
            'days': labels,
            'videos': [videos.filter(uploaded_at__date=day).count() for day in days],
            'analyses': [analyses.filter(created_at__date=day).count() for day in days],
        })


class MyPreferencesView(APIView):
    """Retrieve or update preferences for the authenticated user."""

    def get(self, request):
        preferences, _created = UserPreferences.objects.get_or_create(user=request.user)
        serializer = UserPreferencesReadSerializer(preferences)
        return Response(serializer.data)

    def patch(self, request):
        preferences, _created = UserPreferences.objects.get_or_create(user=request.user)
        serializer = UserPreferencesUpdateSerializer(
            preferences,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserPreferencesReadSerializer(preferences).data)


class VideoListView(APIView):
    """List videos visible to the authenticated instructor/admin."""

    def get(self, request):
        serializer = VideoReadSerializer(owned_videos(request.user), many=True, context={'request': request})
        return Response(serializer.data)


class VideoDetailView(APIView):
    """Retrieve a single uploaded video."""

    def get(self, request, pk):
        video = get_object_or_404(owned_videos(request.user), pk=pk)
        return Response(VideoReadSerializer(video, context={'request': request}).data)


class VideoUploadView(APIView):
    """Upload a video into an explicit or automatically created exam session."""

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            raise ValidationError({'file': 'This field is required.'})

        # Pass request.data straight through — never copy it. Copying a
        # multipart QueryDict deep-copies the uploaded file handle and raises
        # "TypeError: cannot pickle 'BufferedRandom' instances".
        serializer = VideoUploadSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)

        # Resolve the target session. When the client supplies one it is already
        # validated (ownership) by the serializer; otherwise auto-create/reuse a
        # direct-upload session for this instructor.
        session = serializer.validated_data.get('session')
        if session is None:
            session = get_available_upload_session(
                instructor=request.user,
                upload=upload,
                exam_name=request.data.get('exam_name', ''),
                student_identifier=request.data.get('student_identifier', ''),
            )

        video = serializer.save(session=session)
        write_audit_log(
            request,
            AuditLog.ActionType.VIDEO_UPLOADED,
            target_resource=str(video.id),
            metadata={'filename': video.original_filename, 'session': str(video.session_id)},
        )
        return Response(VideoReadSerializer(video, context={'request': request}).data, status=status.HTTP_201_CREATED)


class AnalyzeVideoView(APIView):
    """Queue background analysis for an uploaded video.

    The analysis runs on the django-rq ``default`` queue. When the queue is
    synchronous (``RQ_ASYNC`` off — the dev default) the job finishes before
    this returns, so the full report is included with ``200 OK``. With a real
    Redis-backed worker the job is still ``QUEUED`` on return, so we answer
    ``202 Accepted`` and the client polls the video/report endpoints for
    completion.
    """

    def post(self, request, pk):
        video = get_object_or_404(owned_videos(request.user), pk=pk)
        job = enqueue_analysis(request, video)
        video.refresh_from_db()
        session = video.session

        payload = {
            'job_id': str(job.id),
            'status': job.status,
            'video': VideoReadSerializer(video, context={'request': request}).data,
        }
        if job.status == AnalysisJob.Status.COMPLETED and hasattr(session, 'report'):
            payload['analysis'] = ReportReadSerializer(session.report).data
            payload['alerts'] = list(session.alerts.values(
                'id',
                'timestamp_sec',
                'behavior_type',
                'severity',
                'confidence_score',
                'metadata',
                'created_at',
            ))
            return Response(payload)
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class VideoHistoryView(APIView):
    """List analyzed videos for the History page."""

    def get(self, request):
        queryset = owned_videos(request.user).filter(session__status=ExamSession.Status.COMPLETED)
        serializer = VideoReadSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


class DashboardStatsView(APIView):
    """Aggregate counts used by the dashboard cards."""

    def get(self, request):
        return Response(dashboard_stats_for(request.user))


class DashboardActivityView(APIView):
    """Return seven-day upload and analysis activity for dashboard charts."""

    def get(self, request):
        return Response(activity_series_for(request.user))


class ThresholdsView(APIView):
    """Read global, user-specific, and effective AI thresholds."""

    def get(self, request):
        return Response(build_thresholds_response(request.user))


class MyThresholdsView(APIView):
    """Update the authenticated user's threshold overrides."""

    def patch(self, request):
        return Response(update_user_thresholds(request.user, request.data))


class GlobalThresholdsView(APIView):
    """Update global AI thresholds. Restricted to administrators."""

    def patch(self, request):
        if not is_admin(request.user):
            raise PermissionDenied('Only administrators can update global thresholds.')
        return Response(update_global_thresholds(request, request.data))


class SystemLogsView(APIView):
    """Return paged audit-log rows in the frontend's system-log shape."""

    def get(self, request):
        if not is_admin(request.user):
            raise PermissionDenied('Only administrators can view system logs.')

        page = max(int(request.query_params.get('page', 1)), 1)
        page_size = min(max(int(request.query_params.get('page_size', 50)), 1), 100)
        queryset = AuditLog.objects.select_related('user').all()
        start = (page - 1) * page_size
        end = start + page_size
        results = [
            {
                'id': str(log.id),
                'created_at': log.performed_at,
                'level': 'info',
                'message': f'{log.get_action_display()} {log.target_resource or ""}'.strip(),
                'actor_email': log.user.email if log.user else None,
                'meta': log.metadata,
            }
            for log in queryset[start:end]
        ]
        return Response({
            'count': queryset.count(),
            'page': page,
            'page_size': page_size,
            'results': results,
        })


class SystemMetricsView(APIView):
    """Return lightweight runtime and object-count metrics."""

    def get(self, request):
        if not is_admin(request.user):
            raise PermissionDenied('Only administrators can view system metrics.')
        return Response(system_metrics())


class InstructorOversightView(APIView):
    """Dean-level oversight of every instructor and their activity.

    Returns one row per instructor with aggregate exam/session counts so a
    dean can monitor proctoring activity across the whole platform. Restricted
    to deans (and admins/superusers, who implicitly satisfy the dean check).
    """

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can view instructor oversight.')

        instructors = (
            User.objects.filter(role=User.Role.INSTRUCTOR)
            .annotate(
                exam_count=Count('exams', distinct=True),
                session_count=Count('exams__sessions', distinct=True),
                flagged_sessions=Count(
                    'exams__sessions',
                    filter=Q(exams__sessions__alerts__isnull=False),
                    distinct=True,
                ),
            )
            .order_by('email')
        )
        results = [
            {
                'id': str(instructor.id),
                'email': instructor.email,
                'username': instructor.username,
                'is_active': instructor.is_active,
                'exam_count': instructor.exam_count,
                'session_count': instructor.session_count,
                'flagged_sessions': instructor.flagged_sessions,
            }
            for instructor in instructors
        ]
        return Response(results)


class HallManagementView(APIView):
    """Dean-level view of exam 'halls' across all instructors.

    Each exam is surfaced as a hall with its owning instructor and a breakdown
    of session progress, giving deans a single place to review where exams are
    being run. Restricted to deans (and admins/superusers).
    """

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can manage halls.')

        exams = (
            Exam.objects.select_related('instructor')
            .annotate(
                session_count=Count('sessions', distinct=True),
                completed_count=Count(
                    'sessions',
                    filter=Q(sessions__status=ExamSession.Status.COMPLETED),
                    distinct=True,
                ),
                pending_count=Count(
                    'sessions',
                    filter=Q(sessions__status=ExamSession.Status.PENDING),
                    distinct=True,
                ),
            )
            .order_by('-created_at')
        )
        results = [
            {
                'id': str(exam.id),
                'name': exam.name,
                'description': exam.description,
                'instructor_email': exam.instructor.email if exam.instructor else None,
                'session_count': exam.session_count,
                'completed_count': exam.completed_count,
                'pending_count': exam.pending_count,
                'created_at': exam.created_at,
            }
            for exam in exams
        ]
        return Response(results)

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AnalysisJob, AuditLog, AutoExamSession, Exam, ExamSession, Notification,
    UserPreferences, Video, WorkspaceInvite,
)
from .selectors import (
    actionable_exams, assigned_supervisor_ids, can_supervise_exam,
    is_admin, is_dean, owned_students, owned_videos, recent_day_window, users_visible_to,
)
from .serializers import (
    AutoExamSessionCreateSerializer,
    AutoExamSessionReadSerializer,
    ExamReadSerializer,
    NotificationSerializer,
    ReportReadSerializer,
    StudentSerializer,
    UserPreferencesReadSerializer,
    UserPreferencesUpdateSerializer,
    UserReadSerializer,
    UserUpdateSerializer,
    VideoReadSerializer,
    VideoUploadSerializer,
    WorkspaceInviteSerializer,
)
from .services import (
    activity_series_for,
    build_thresholds_response,
    dashboard_stats_for,
    enqueue_analysis,
    get_available_upload_session,
    recording_session_for,
    resolve_calendar_exam,
    respond_to_workspace_invite,
    send_workspace_invite,
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
    """Retrieve or delete a single uploaded video."""

    def get(self, request, pk):
        video = get_object_or_404(owned_videos(request.user), pk=pk)
        return Response(VideoReadSerializer(video, context={'request': request}).data)

    def delete(self, request, pk):
        # Uploader or dean/admin only — looked up broadly (not just owned_videos)
        # so an invited supervisor who uploaded, or a dean, can remove it. This
        # frees the session so a new video can be uploaded again.
        video = get_object_or_404(
            Video.objects.select_related('uploaded_by', 'session__exam__instructor'),
            pk=pk,
        )
        if not (is_dean(request.user) or video.uploaded_by_id == request.user.id):
            raise PermissionDenied('Only the uploader or the dean can delete this video.')
        write_audit_log(
            request,
            AuditLog.ActionType.VIDEO_DELETED,
            target_resource=str(video.id),
        )
        video.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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

        # Resolve the target session.
        # 1. Calendar flow (exam_id): use the exam's single canonical recording
        #    session, enforce supervisor permission, and reject a second upload
        #    with 409 so the "already uploaded" button stays disabled.
        # 2. Explicit session: already ownership-validated by the serializer.
        # 3. Otherwise: auto-create/reuse a direct-upload session.
        exam_id = request.data.get('exam_id')
        session = serializer.validated_data.get('session')
        if exam_id:
            exam = get_object_or_404(Exam, pk=exam_id)
            if not can_supervise_exam(request.user, exam):
                raise PermissionDenied(
                    'Only assigned supervisors or the dean can upload for this exam.'
                )
            session = recording_session_for(exam)
            if hasattr(session, 'video'):
                return Response(
                    {'detail': 'A video has already been uploaded for this session.'},
                    status=status.HTTP_409_CONFLICT,
                )
        elif session is None:
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


class NotificationListView(generics.ListAPIView):
    """List the authenticated user's notifications (most recent first)."""

    serializer_class = NotificationSerializer

    def get_queryset(self):
        return self.request.user.notifications.all()


class UnreadNotificationsView(APIView):
    """Return the count of unread notifications for the current user."""

    def get(self, request):
        count = request.user.notifications.filter(is_read=False).count()
        return Response({'unread': count})


class MarkNotificationsReadView(APIView):
    """Mark every unread notification for the current user as read."""

    def patch(self, request):
        updated = request.user.notifications.filter(is_read=False).update(is_read=True)
        return Response({'updated': updated})


class WorkspaceInviteView(APIView):
    """Dean workspace invites: list the ones you've sent, or send a new one."""

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can manage workspace invites.')
        invites = (
            WorkspaceInvite.objects
            .select_related('instructor', 'dean', 'exam')
            .filter(dean=request.user)
        )
        return Response(WorkspaceInviteSerializer(invites, many=True).data)

    def post(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can send workspace invites.')

        instructor_email = (request.data.get('instructor_email') or '').strip()
        exam_id = request.data.get('exam_id')
        if not instructor_email:
            raise ValidationError({'instructor_email': 'This field is required.'})
        if not exam_id:
            raise ValidationError({'exam_id': 'This field is required.'})

        instructor = User.objects.filter(email__iexact=instructor_email).first()
        if instructor is None:
            raise ValidationError({'instructor_email': 'No user found with that email.'})

        exam = get_object_or_404(Exam, pk=exam_id)
        invite = send_workspace_invite(request.user, instructor, exam)
        return Response(
            WorkspaceInviteSerializer(invite).data,
            status=status.HTTP_201_CREATED,
        )


class InviteRespondView(APIView):
    """Public accept/decline endpoint reached from the invite email link.

    Authentication is intentionally disabled: possession of the secret invite
    token authorises the response, so the instructor never has to log in.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    accepted = True  # overridden per-URL via .as_view(accepted=...)

    def get(self, request, token):
        invite = get_object_or_404(
            WorkspaceInvite.objects.select_related('instructor', 'dean', 'exam'),
            token=token,
        )
        already_responded = invite.status != WorkspaceInvite.Status.PENDING
        respond_to_workspace_invite(invite, self.accepted)
        return Response({
            'status': invite.status,
            'already_responded': already_responded,
            'exam_name': invite.exam.name,
            'instructor_email': invite.instructor.email,
        })


class AutoSessionListCreateView(generics.ListCreateAPIView):
    """List the current user's auto recording sessions, or schedule a new one."""

    def get_queryset(self):
        queryset = AutoExamSession.objects.select_related('exam', 'instructor')
        if is_admin(self.request.user):
            return queryset
        return queryset.filter(instructor=self.request.user)

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return AutoExamSessionCreateSerializer
        return AutoExamSessionReadSerializer

    def get_serializer_context(self):
        return {**super().get_serializer_context(), 'request': self.request}

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        read = AutoExamSessionReadSerializer(instance, context={'request': request})
        return Response(read.data, status=status.HTTP_201_CREATED)


class AutoSessionDetailView(generics.DestroyAPIView):
    """Cancel (delete) one of the current user's auto recording sessions."""

    serializer_class = AutoExamSessionReadSerializer

    def get_queryset(self):
        queryset = AutoExamSession.objects.all()
        if is_admin(self.request.user):
            return queryset
        return queryset.filter(instructor=self.request.user)


def _exam_action_payload(request, exam):
    """Build the calendar action/status payload for an exam's recording session.

    Reports who (if anyone) has uploaded a video, the assigned supervisors, and
    what the requesting user is allowed to do (manage / delete).
    """
    session = recording_session_for(exam)
    video = getattr(session, 'video', None)
    supervisor_emails = list(
        User.objects.filter(id__in=assigned_supervisor_ids(exam))
        .order_by('email')
        .values_list('email', flat=True)
    )
    uploaded_by = None
    can_delete = False
    video_id = None
    if video is not None:
        video_id = str(video.id)
        if video.uploaded_by_id:
            uploaded_by = video.uploaded_by.email
        elif exam.instructor_id:
            uploaded_by = exam.instructor.email
        can_delete = is_dean(request.user) or video.uploaded_by_id == request.user.id
    return {
        'exam_id': str(exam.id),
        'session_id': str(session.id),
        'name': exam.name,
        'owner_email': exam.instructor.email if exam.instructor_id else None,
        'supervisor_emails': supervisor_emails,
        'can_manage': can_supervise_exam(request.user, exam),
        'has_video': video is not None,
        'uploaded_by': uploaded_by,
        'video_id': video_id,
        'can_delete': can_delete,
    }


class ExamResolveView(APIView):
    """Resolve a calendar entry's name to a real shared Exam + its recording status.

    The frontend calls this when an exam's modal opens so the Manual Analysis /
    Auto Recording buttons can act on real backend records.
    """

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            raise ValidationError({'name': 'This field is required.'})
        exam = resolve_calendar_exam(request.user, name)
        return Response(_exam_action_payload(request, exam))


class SessionVideoStatusView(APIView):
    """Video status for an exam's canonical recording session (calendar buttons)."""

    def get(self, request, exam_id):
        exam = get_object_or_404(actionable_exams(request.user), pk=exam_id)
        return Response(_exam_action_payload(request, exam))


class UserLookupView(APIView):
    """Look up a registered user by email — dean/admin only.

    Powers the dean's "invite instructor" modal: the dean types only an email,
    and this confirms it belongs to a real account and returns the person's
    display name / username so they can be shown (read-only) before the invite
    is sent. The frontend decides what to do with non-instructor roles, so this
    endpoint returns the account regardless of role and only 404s when no
    account has that email at all.
    """

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can look up instructors.')

        email = (request.query_params.get('email') or '').strip()
        if not email:
            raise ValidationError({'email': 'This query parameter is required.'})

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            return Response(
                {'detail': 'No user found with this email address.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        display_name = (user.get_full_name() or '').strip() or user.username
        return Response({
            'id': str(user.id),
            'email': user.email,
            'username': user.username,
            'display_name': display_name,
            'role': user.role,
        })


class ExamListView(APIView):
    """List exams the requesting user can assign instructors to.

    Deans/admins see every exam; any other user sees the exams they own or have
    accepted an invite to supervise. Used to populate the invite modal's exam
    picker entirely from the backend (no mock data, no client storage).
    """

    def get(self, request):
        exams = actionable_exams(request.user).order_by('-created_at')
        return Response(ExamReadSerializer(exams, many=True).data)

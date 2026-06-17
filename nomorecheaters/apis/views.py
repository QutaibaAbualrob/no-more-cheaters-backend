from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Alert, AnalysisJob, AuditLog, AutoExamSession, Exam, ExamSession, Notification,
    UserPreferences, Video, Workspace, WorkspaceInvite, WorkspaceMembership,
)
from .selectors import (
    actionable_exams, assigned_supervisor_ids, calendar_exams, can_supervise_exam,
    can_view_report, is_admin, is_dean, owned_sessions, owned_students, owned_videos,
    recent_day_window, users_visible_to, visible_sessions, visible_videos,
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
    WorkspaceMemberSerializer,
    WorkspaceSerializer,
    WorkspaceWriteSerializer,
)
from .services import (
    activity_series_for,
    build_thresholds_response,
    dashboard_stats_for,
    delete_exam_media,
    email_workspace_invite,
    enqueue_analysis,
    exam_supervisor_users,
    get_available_upload_session,
    notify_users,
    recording_session_for,
    resolve_calendar_exam,
    respond_to_workspace_invite,
    send_workspace_invite,
    sync_exam_supervisors,
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
        response = Response(
            VideoReadSerializer(video, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )
        # Location points at the newly created resource so the client can follow
        # it without parsing the body for the id (RFC 7231 §7.1.2).
        response['Location'] = request.build_absolute_uri(
            reverse('videos_detail', kwargs={'pk': video.id}))
        return response


class AnalyzeVideoView(APIView):
    """Queue background analysis for an uploaded video.

    The analysis runs on the django-rq ``default`` queue. When the queue is
    synchronous (``RQ_ASYNC`` off — the dev default) the job finishes before
    this returns, so the full report is included with ``200 OK``. With a real
    Redis-backed worker the job is still ``QUEUED`` on return, so we answer
    ``202 Accepted`` and the client polls the video/report endpoints for
    completion.
    """

    # Hint (seconds) for how soon a client should poll after a 202. The job is
    # typically still QUEUED on return with a real worker; a few seconds avoids
    # a tight poll loop without making the UI feel stalled.
    RETRY_AFTER_SECONDS = 5

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

        response = Response(payload, status=status.HTTP_202_ACCEPTED)
        # Tell the client when to poll and where: Retry-After is the suggested
        # delay, Location is the resource whose status it should re-fetch.
        response['Retry-After'] = str(self.RETRY_AFTER_SECONDS)
        response['Location'] = request.build_absolute_uri(
            reverse('videos_detail', kwargs={'pk': video.id}))
        return response


class VideoHistoryView(APIView):
    """List analyzed videos for the History page (workspace-scoped)."""

    def get(self, request):
        queryset = visible_videos(request.user).filter(
            session__status=ExamSession.Status.COMPLETED,
        )
        serializer = VideoReadSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


class AnalysisSessionsView(APIView):
    """All analyzed sessions the user may view — drives the Analysis page exam list.

    Returns sessions whose status is PROCESSING / COMPLETED / FAILED (anything
    that has been or is being analyzed), workspace-scoped via
    :func:`visible_sessions`. Ordered newest-first. Each row carries enough for
    the frontend's grouped list and status dot.
    """

    def get(self, request):
        sessions = (
            visible_sessions(request.user)
            .exclude(status=ExamSession.Status.PENDING)
            .order_by('-created_at')
        )
        rows = []
        for session in sessions:
            report = getattr(session, 'report', None)
            job = getattr(session, 'analysis_job', None)
            rows.append({
                'session_id': str(session.id),
                'exam_name': session.exam.name,
                # No scheduled date exists server-side (calendar schedule is
                # client-only); the analysis date is the meaningful timestamp.
                'exam_date': session.created_at,
                'status': session.status,
                'analysis_status': job.status if job else session.status,
                'total_alerts': report.total_alerts if report else 0,
                'created_at': session.created_at,
            })
        return Response(rows)


def _person_sort_key(person_id):
    """Sort ``person_1``, ``person_2`` … numerically (non-numeric ids last)."""
    try:
        return (0, int(str(person_id).rsplit('_', 1)[-1]))
    except (ValueError, IndexError):
        return (1, 0)


def _alert_payload(alert, request):
    """Serialize one alert with absolute media URLs and review state.

    Shared by the report endpoint and the dismiss/flag endpoints so all three
    return the same alert shape.
    """
    def absolute(url):
        return request.build_absolute_uri(url) if url else None

    # Copy metadata so we can hand back an absolute crop_url without mutating
    # the stored (relative) value. crop_url is the clean face avatar; snapshot_url
    # is the full frame with the green box drawn on the flagged person.
    metadata = dict(alert.metadata or {})
    crop_url = absolute(metadata.get('crop_url'))
    if metadata.get('crop_url'):
        metadata['crop_url'] = crop_url
    return {
        'id': str(alert.id),
        'behavior_type': alert.behavior_type,
        'behavior_label': alert.get_behavior_type_display(),
        'severity': alert.severity,
        'confidence_score': alert.confidence_score,
        'timestamp_sec': alert.timestamp_sec,
        'snapshot_url': absolute(alert.snapshot_url),
        'clip_url': absolute(alert.clip_url),
        'crop_url': crop_url,
        'metadata': metadata,
        'is_reviewed': alert.is_reviewed,
        'reviewed_at': alert.reviewed_at,
        'flagged': bool(metadata.get('flagged')),
    }


class SessionReportView(APIView):
    """Detailed analysis report for one session, grouped by detected person.

    Feeds the Analysis Reports page (deep-linked from History). Each alert
    carries its face crop, 3-second clip, behaviour label, severity, confidence
    and timestamp; alerts are grouped under the person they were attributed to
    so multi-student recordings get one section per student.
    """

    def get(self, request, session_id):
        session = get_object_or_404(
            ExamSession.objects.select_related(
                'exam', 'exam__instructor', 'video', 'report', 'analysis_job',
            ),
            pk=session_id,
        )
        # Workspace-scoped: the exam owner, a co-member of a shared workspace, an
        # accepted per-exam supervisor, the workspace's dean, or an admin may view.
        if not can_view_report(request.user, session.exam):
            raise PermissionDenied('You do not have access to this session.')

        def absolute(url):
            return request.build_absolute_uri(url) if url else None

        video = getattr(session, 'video', None)
        job = getattr(session, 'analysis_job', None)
        report = getattr(session, 'report', None)

        # Base envelope — always 200 so the client can poll for status while a
        # job is still QUEUED/PROCESSING (or surface a FAILED error + retry).
        analysis_status = job.status if job else ('COMPLETED' if report else 'PENDING')
        base = {
            'session_id': str(session.id),
            'exam_name': session.exam.name,
            'student_identifier': session.student_identifier,
            'status': session.status,
            'analysis_status': analysis_status,
            'error_message': job.error_message if job else '',
            'video_id': str(video.id) if video else None,
            'video_url': absolute(video.file.url) if video and video.file else None,
            # The annotated full-length video (boxes/labels drawn on flagged
            # frames), produced by the analysis pipeline. None until analysis runs.
            'annotated_video_url': absolute((job.metadata or {}).get('annotated_video_url')) if job else None,
            # Per-person box tracks (source-frame pixel coords) for the live
            # client-side overlay drawn over the original video. None for jobs
            # analysed before this existed; the player then uses the annotated
            # video instead. Coords are not URLs, so they pass through verbatim.
            'overlay': (job.metadata or {}).get('overlay') if job else None,
        }

        if report is None:
            base.update({
                'overall_cheating_probability': 0,
                'total_alerts': 0,
                'alerts_by_type': {},
                'summary': '',
                'generated_at': None,
                'report': None,
                'person_count': 0,
                'persons': [],
            })
            return Response(base)

        groups = {}
        for alert in session.alerts.all().order_by('timestamp_sec', 'created_at'):
            person_id = (alert.metadata or {}).get('person_id') or 'person_1'
            groups.setdefault(person_id, []).append(_alert_payload(alert, request))

        persons = []
        for person_id in sorted(groups, key=_person_sort_key):
            payloads = groups[person_id]
            # Section avatar = this person's highest-confidence face crop (clean,
            # no box); fall back to the boxed full-frame snapshot if no crop.
            ranked = sorted(payloads, key=lambda p: p['confidence_score'], reverse=True)
            face_url = None
            for payload in ranked:
                if payload.get('crop_url'):
                    face_url = payload['crop_url']
                    break
            if face_url is None:
                for payload in ranked:
                    if payload['snapshot_url']:
                        face_url = payload['snapshot_url']
                        break
            label = 'Person ' + str(person_id).rsplit('_', 1)[-1]
            persons.append({
                'person_id': person_id,
                'label': label,
                'face_url': face_url,
                'alert_count': len(payloads),
                'alerts': payloads,
            })

        base.update({
            'overall_cheating_probability': report.overall_cheating_probability,
            'total_alerts': report.total_alerts,
            'alerts_by_type': report.alerts_by_type,
            'summary': report.summary,
            'generated_at': report.generated_at,
            'report': ReportReadSerializer(report).data,
            'person_count': len(persons),
            'persons': persons,
        })
        return Response(base)


class AlertReviewView(APIView):
    """Dismiss or flag a single alert (assigned supervisor / dean / admin).

    ``action='dismiss'`` marks the alert reviewed; ``action='flag'`` marks it
    reviewed AND records ``metadata['flagged'] = True`` for the "Flagged" badge.
    """

    action = 'dismiss'  # overridden per-URL via .as_view(action=...)

    def patch(self, request, alert_id):
        alert = get_object_or_404(
            Alert.objects.select_related('session__exam__instructor'), pk=alert_id,
        )
        if not can_supervise_exam(request.user, alert.session.exam):
            raise PermissionDenied('You do not have access to this alert.')

        if self.action == 'flag':
            metadata = dict(alert.metadata or {})
            metadata['flagged'] = True
            alert.metadata = metadata
            alert.is_reviewed = True
            alert.reviewed_by = request.user
            alert.reviewed_at = timezone.now()
            alert.save(update_fields=['metadata', 'is_reviewed', 'reviewed_by', 'reviewed_at'])
        else:
            alert.mark_reviewed(request.user)

        return Response(_alert_payload(alert, request))


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


def accepted_instructor_rows(user):
    """Rows for instructors with an ACCEPTED workspace membership.

    A membership only exists once an invite has been accepted (and is deleted on
    removal / never created on decline), so this is the single source of truth
    for "which instructors a dean may use" — every picker and the oversight
    roster read from it, ensuring a declined or removed instructor disappears
    everywhere. Scoped to the dean's own workspaces; admins see members across
    all workspaces. Each row carries the picker fields plus oversight stats.
    """
    memberships = WorkspaceMembership.objects.all()
    if not is_admin(user):
        memberships = memberships.filter(workspace__owner=user)

    earliest_joined = {}
    for instructor_id, joined_at in memberships.values_list('instructor_id', 'joined_at'):
        if instructor_id not in earliest_joined or joined_at < earliest_joined[instructor_id]:
            earliest_joined[instructor_id] = joined_at

    if not earliest_joined:
        return []

    users = (
        User.objects.filter(id__in=earliest_joined.keys())
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
    rows = []
    for instructor in users:
        display_name = (instructor.get_full_name() or '').strip() or instructor.username
        rows.append({
            'id': str(instructor.id),
            'user_id': str(instructor.id),
            'email': instructor.email,
            'username': instructor.username,
            'display_name': display_name,
            'role': instructor.role,
            'is_active': instructor.is_active,
            'joined_at': earliest_joined[instructor.id],
            'exam_count': instructor.exam_count,
            'session_count': instructor.session_count,
            'flagged_sessions': instructor.flagged_sessions,
        })
    return rows


class InstructorOversightView(APIView):
    """Dean-level oversight of the dean's accepted-member instructors.

    Returns one row per instructor — but only instructors who have ACCEPTED a
    workspace invite from this dean (admins see all workspace members). It never
    exposes unaffiliated accounts.
    """

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can view instructor oversight.')
        return Response(accepted_instructor_rows(request.user))


class AllWorkspaceMembersView(APIView):
    """Every unique instructor accepted into any of the dean's workspaces.

    The global instructor picker (exam assignment, calendar supervisors, etc.)
    reads from here so it can only ever offer real, accepted members.
    """

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can list workspace members.')
        return Response(accepted_instructor_rows(request.user))


class MyWorkspacesView(APIView):
    """Workspaces the CURRENT user belongs to (as an accepted member).

    Powers the frontend's ``hasWorkspace`` gate: a freshly-registered instructor
    with no accepted membership gets ``has_workspace: false`` and is shown the
    "ask your dean for an invite" empty state instead of an empty dashboard.
    Works for any authenticated user (unlike the dean-only members endpoint).
    """

    def get(self, request):
        memberships = (
            WorkspaceMembership.objects
            .filter(instructor=request.user)
            .select_related('workspace', 'workspace__owner')
            .order_by('joined_at')
        )
        rows = [
            {
                'workspace_id': str(m.workspace_id),
                'workspace_name': m.workspace.name,
                'owner_email': m.workspace.owner.email if m.workspace.owner_id else None,
                'joined_at': m.joined_at,
            }
            for m in memberships
        ]
        return Response({'has_workspace': len(rows) > 0, 'workspaces': rows})


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
    """List the authenticated user's notifications (most recent first).

    Dismissed notifications are excluded — they stay in the database for audit
    but never appear in the bell dropdown again.
    """

    serializer_class = NotificationSerializer

    def get_queryset(self):
        return self.request.user.notifications.filter(is_dismissed=False)


class UnreadNotificationsView(APIView):
    """Return the count of unread (and not-dismissed) notifications."""

    def get(self, request):
        count = request.user.notifications.filter(
            is_read=False, is_dismissed=False,
        ).count()
        return Response({'unread': count})


class MarkNotificationsReadView(APIView):
    """Mark every unread notification for the current user as read.

    Read state is independent of dismissal: this only clears the unread dot and
    never hides a notification from the list.
    """

    def patch(self, request):
        updated = request.user.notifications.filter(is_read=False).update(is_read=True)
        return Response({'updated': updated})


class DismissNotificationView(APIView):
    """Dismiss a single notification (hide it from the list permanently)."""

    def patch(self, request, pk):
        notification = get_object_or_404(
            request.user.notifications, pk=pk,
        )
        if not notification.is_dismissed:
            notification.is_dismissed = True
            notification.save(update_fields=['is_dismissed'])
        return Response({'id': str(notification.id), 'is_dismissed': True})


class DismissAllNotificationsView(APIView):
    """Dismiss all *read* notifications for the current user ("Clear all").

    Unread notifications are left untouched so the user never loses something
    they have not seen yet.
    """

    def patch(self, request):
        updated = request.user.notifications.filter(
            is_read=True, is_dismissed=False,
        ).update(is_dismissed=True)
        return Response({'updated': updated})


def _owned_workspaces(user):
    """Scope workspaces to their owning dean; admins see every workspace."""
    queryset = Workspace.objects.select_related('owner').prefetch_related(
        'memberships__instructor',
    )
    if is_admin(user):
        return queryset
    return queryset.filter(owner=user)


class WorkspaceListCreateView(APIView):
    """List the dean's named workspaces, or create a new one."""

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can manage workspaces.')
        workspaces = _owned_workspaces(request.user)
        return Response(WorkspaceSerializer(workspaces, many=True).data)

    def post(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can create workspaces.')
        serializer = WorkspaceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workspace = Workspace.objects.create(
            owner=request.user, name=serializer.validated_data['name'],
        )
        return Response(
            WorkspaceSerializer(workspace).data,
            status=status.HTTP_201_CREATED,
        )


class WorkspaceDetailView(APIView):
    """Rename or delete one of the dean's workspaces."""

    def _get(self, request, pk):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can manage workspaces.')
        return get_object_or_404(_owned_workspaces(request.user), pk=pk)

    def patch(self, request, pk):
        workspace = self._get(request, pk)
        serializer = WorkspaceWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        workspace.name = serializer.validated_data['name']
        workspace.save(update_fields=['name'])
        return Response(WorkspaceSerializer(workspace).data)

    def delete(self, request, pk):
        workspace = self._get(request, pk)
        workspace.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMembersView(APIView):
    """List members of a workspace."""

    def get(self, request, workspace_id):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can view workspace members.')
        workspace = get_object_or_404(_owned_workspaces(request.user), pk=workspace_id)
        memberships = workspace.memberships.select_related('instructor').all()
        return Response(WorkspaceMemberSerializer(memberships, many=True).data)


class WorkspaceMemberDetailView(APIView):
    """Remove an instructor from a workspace."""

    def delete(self, request, workspace_id, user_id):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can manage workspace members.')
        workspace = get_object_or_404(_owned_workspaces(request.user), pk=workspace_id)
        membership = get_object_or_404(
            WorkspaceMembership, workspace=workspace, instructor_id=user_id,
        )
        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceInviteView(APIView):
    """Dean workspace invites: list the ones you've sent, or send a new one.

    Inviting is a pure workspace action — it never touches exams. The invite is
    created with ``exam = null``; exam/supervisor assignment happens separately
    in the Calendar, drawing only from accepted workspace members.
    """

    def get(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can manage workspace invites.')
        invites = (
            WorkspaceInvite.objects
            .select_related('instructor', 'dean', 'exam', 'workspace')
            .filter(dean=request.user)
        )
        return Response(WorkspaceInviteSerializer(invites, many=True).data)

    def post(self, request):
        if not is_dean(request.user):
            raise PermissionDenied('Only deans can send workspace invites.')

        # `invitee_email` is the canonical field; `instructor_email` is accepted
        # as an alias for older callers.
        invitee_email = (
            request.data.get('invitee_email')
            or request.data.get('instructor_email')
            or ''
        ).strip()
        workspace_id = request.data.get('workspace_id')
        if not invitee_email:
            raise ValidationError({'invitee_email': 'This field is required.'})
        if not workspace_id:
            raise ValidationError({'workspace_id': 'This field is required.'})

        workspace = _owned_workspaces(request.user).filter(pk=workspace_id).first()
        if workspace is None:
            raise NotFound('Workspace not found')

        invitee = User.objects.filter(email__iexact=invitee_email).first()
        if invitee is None:
            raise NotFound('No user found with this email')
        if invitee.id == request.user.id:
            raise ValidationError('You cannot invite yourself')
        if invitee.is_superuser or invitee.role == User.Role.ADMIN:
            raise ValidationError('Cannot invite admin users')
        if invitee.role not in (User.Role.INSTRUCTOR, User.Role.DEAN):
            raise ValidationError('Only instructors or deans can be invited')
        if WorkspaceMembership.objects.filter(workspace=workspace, instructor=invitee).exists():
            raise ValidationError('This user is already a member of this workspace')

        # Already-pending invite: resend the email instead of erroring (200).
        pending = WorkspaceInvite.objects.filter(
            workspace=workspace,
            instructor=invitee,
            status=WorkspaceInvite.Status.PENDING,
        ).first()
        if pending is not None:
            email_workspace_invite(pending)
            data = WorkspaceInviteSerializer(pending).data
            data['detail'] = 'Invite resent'
            return Response(data, status=status.HTTP_200_OK)

        # exam stays null — assignment is a separate Calendar step.
        invite = send_workspace_invite(request.user, invitee, workspace=workspace)
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

    def post(self, request, token):
        # POST, not GET: responding to an invite mutates state (joins a
        # workspace / sets the invite status), so it must not happen on a GET.
        # Email security scanners and link-preview bots issue GET requests when
        # they pre-fetch the link in the invite email; with a GET handler that
        # silently accepted/declined the invite before the human ever clicked
        # (RFC 7231 §4.2.1 — GET must be safe). The frontend page now POSTs.
        invite = get_object_or_404(
            WorkspaceInvite.objects.select_related('instructor', 'dean', 'exam', 'workspace'),
            token=token,
        )
        already_responded = invite.status != WorkspaceInvite.Status.PENDING
        respond_to_workspace_invite(invite, self.accepted)

        if already_responded:
            detail = 'You have already responded to this invite'
        elif self.accepted:
            detail = 'You have joined the workspace successfully'
        else:
            detail = 'You have declined the invite'

        return Response({
            'detail': detail,
            'status': invite.status,
            'already_responded': already_responded,
            'target_name': invite.target_name,
            'workspace_name': invite.workspace.name if invite.workspace_id else None,
            'exam_name': invite.exam.name if invite.exam_id else None,
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


def _apply_exam_schedule(exam, data):
    """Set calendar schedule/presentation fields on *exam* from request *data*.

    Only keys present in *data* are touched. Date/time arrive as ISO strings
    ('YYYY-MM-DD' / 'HH:MM'); a blank value clears the field. Returns the list of
    changed field names (so the caller can ``save(update_fields=...)``).
    """
    from django.utils.dateparse import parse_date, parse_time

    changed = []
    if 'course' in data:
        exam.course = (data.get('course') or '')[:255]
        changed.append('course')
    if 'hall' in data:
        exam.hall = (data.get('hall') or '')[:255]
        changed.append('hall')
    if data.get('color'):
        exam.color = str(data['color'])[:20]
        changed.append('color')
    if data.get('recording_mode') in dict(Exam.RecordingMode.choices):
        exam.recording_mode = data['recording_mode']
        changed.append('recording_mode')
    if 'date' in data or 'scheduled_date' in data:
        raw = data.get('date') or data.get('scheduled_date')
        exam.scheduled_date = parse_date(raw) if raw else None
        changed.append('scheduled_date')
    if 'start_time' in data:
        raw = data.get('start_time')
        exam.start_time = parse_time(raw) if raw else None
        changed.append('start_time')
    if 'end_time' in data:
        raw = data.get('end_time')
        exam.end_time = parse_time(raw) if raw else None
        changed.append('end_time')
    return changed


class CalendarExamsView(APIView):
    """The shared exam calendar: list visible exams, or schedule a new one.

    * **GET** — every authenticated user gets the exams on their calendar
      (:func:`calendar_exams`): a dean sees their workspace's exams; an instructor
      sees their own plus every exam a dean has assigned them to supervise.
    * **POST** — dean/admin only. Creates a scheduled exam owned by the dean and
      assigns the given ``supervisor_ids`` immediately (each gets an
      EXAM_ASSIGNED notification and the exam appears on their calendar at once).

    This replaces the old client-cookie ("mock") calendar so exams are real,
    shared backend records — visible across users and devices.
    """

    def get(self, request):
        exams = calendar_exams(request.user).order_by('scheduled_date', 'start_time', '-created_at')
        return Response(ExamReadSerializer(exams, many=True).data)

    def post(self, request):
        if not (is_admin(request.user) or is_dean(request.user)):
            raise PermissionDenied('Only deans or administrators can schedule exams.')

        name = (request.data.get('name') or '').strip()
        if not name:
            raise ValidationError({'name': 'Exam name is required.'})

        exam = Exam(
            instructor=request.user,
            name=name,
            description=request.data.get('description') or '',
        )
        _apply_exam_schedule(exam, request.data)
        exam.save()

        schedule = {
            'date': request.data.get('date') or request.data.get('scheduled_date'),
            'start_time': request.data.get('start_time'),
            'end_time': request.data.get('end_time'),
            'hall': request.data.get('hall'),
        }
        sync_exam_supervisors(request.user, exam, request.data.get('supervisor_ids'), schedule=schedule)
        return Response(ExamReadSerializer(exam).data, status=status.HTTP_201_CREATED)


class ExamDetailView(APIView):
    """Edit/delete an exam, notifying every assigned supervisor.

    * **PATCH (edit)** — the exam owner, a dean, or an admin. Updates the name
      and notifies supervisors of the new schedule.
    * **DELETE (cancel)** — a dean or an admin only (never a plain instructor).
      Removes the exam (cascading to its sessions/videos/alerts/reports), clears
      the media files from disk, audits the action, and notifies supervisors.

    Schedule fields (date/start_time/end_time/hall) live in the calendar client;
    they are accepted here only to compose the notification text. Recipients are
    the exam owner, accepted-invite supervisors, auto-session instructors, and
    any extra ``supervisor_ids`` the request supplies.
    """

    def patch(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        can_edit = (
            is_admin(request.user)
            or is_dean(request.user)
            or exam.instructor_id == request.user.id
        )
        if not can_edit:
            raise PermissionDenied('You do not have permission to edit this exam.')

        changed = ['updated_at']
        name = request.data.get('name')
        if name is not None and str(name).strip():
            exam.name = str(name).strip()
            changed.append('name')
        if 'description' in request.data:
            exam.description = request.data.get('description') or ''
            changed.append('description')
        # Persist any calendar schedule/presentation fields that were sent.
        changed.extend(_apply_exam_schedule(exam, request.data))
        exam.save(update_fields=list(dict.fromkeys(changed)))

        # Re-assign supervisors (dean/admin only): newly-added supervisors get an
        # EXAM_ASSIGNED notification from sync; everyone else gets EXAM_UPDATED.
        added = []
        if 'supervisor_ids' in request.data and (is_admin(request.user) or is_dean(request.user)):
            schedule = {
                'date': request.data.get('date') or request.data.get('scheduled_date'),
                'start_time': request.data.get('start_time'),
                'end_time': request.data.get('end_time'),
                'hall': request.data.get('hall'),
            }
            added = sync_exam_supervisors(request.user, exam, request.data.get('supervisor_ids'), schedule=schedule)

        added_ids = {u.id for u in added}
        supervisors = [
            u for u in exam_supervisor_users(exam, request.data.get('supervisor_ids'))
            if u.id not in added_ids
        ]
        new_date = request.data.get('date') or 'the same date'
        start_time = request.data.get('start_time') or request.data.get('time') or 'the same time'
        end_time = request.data.get('end_time') or ''
        hall = request.data.get('hall') or 'the same hall'
        time_range = f'{start_time}–{end_time}' if end_time else start_time
        notify_users(
            supervisors,
            Notification.NotifType.EXAM_UPDATED,
            f'Exam updated: {exam.name}',
            f'"{exam.name}" has been updated. '
            f'New date: {new_date}, time: {time_range}, hall: {hall}.',
            metadata={'exam_id': str(exam.id)},
        )
        return Response(ExamReadSerializer(exam).data)

    def delete(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        if not (is_admin(request.user) or is_dean(request.user)):
            raise PermissionDenied('Only deans or administrators can delete exams.')

        # Resolve recipients and capture the message fields *before* deleting —
        # the supervisor links and the exam's own attributes are gone once the
        # cascade runs. We only *send* after the delete succeeds (see below).
        supervisors = exam_supervisor_users(exam, request.data.get('supervisor_ids'))
        name = exam.name
        exam_id = str(exam.id)
        date = request.data.get('date') or 'its scheduled date'
        time = request.data.get('time') or 'its scheduled time'

        # Delete first, notify last (M11). Media goes before the DB cascade so the
        # rows pointing at the files still exist while the files are removed; the
        # "exam cancelled" emails are sent only once the exam is actually gone, so
        # a disk error mid-delete never tells recipients an exam was removed while
        # it still exists.
        delete_exam_media(exam)
        write_audit_log(
            request,
            AuditLog.ActionType.EXAM_DELETED,
            target_resource=f'exam:{exam_id}',
            metadata={'name': name},
        )
        exam.delete()

        notify_users(
            supervisors,
            Notification.NotifType.EXAM_CANCELLED,
            f'Exam cancelled: {name}',
            f'The exam "{name}" scheduled for {date} at {time} has been deleted. '
            f'All sessions and uploaded videos were removed. This cannot be undone.',
            metadata={'exam_name': name},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

"""
DRF serializers for the No More Cheaters REST API.

Each domain model has a **Read** serializer (all fields read-only, safe for
list/detail responses) and, where applicable, a **Create** or **Update**
serializer that carries validation and authorisation logic.

Common authorisation pattern
---------------------------
Most write serializers include a ``validate_<fk>`` method that ensures the
requesting user is either:

* The instructor who owns the related exam, **or**
* An administrator (``role == ADMIN`` / ``is_superuser``).
"""

import hashlib
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import UserDetailsSerializer
from rest_framework import serializers

from .models import (
    Alert, AnalysisJob, AuditLog, AutoExamSession, Exam, ExamSession, Notification,
    Report, Student, SystemSettings, UserPreferences, Video, Workspace,
    WorkspaceInvite, WorkspaceMembership,
)


User = get_user_model()


class CustomUserDetailsSerializer(UserDetailsSerializer):
    """User details for dj-rest-auth's ``/api/auth/user/`` endpoint.

    The library default omits our custom ``role`` field, which the frontend
    needs to route ADMIN / DEAN / INSTRUCTOR correctly. We extend the default
    field set with a read-only ``role`` so the SPA never has to guess the role
    (and can never downgrade a DEAN to INSTRUCTOR).
    """

    class Meta(UserDetailsSerializer.Meta):
        fields = tuple(UserDetailsSerializer.Meta.fields) + ('role',)
        read_only_fields = tuple(UserDetailsSerializer.Meta.read_only_fields) + ('role',)


class CustomRegisterSerializer(RegisterSerializer):
    """Registration serializer that also persists the chosen role.

    Fixes the bug where a user who picks "Dean" at signup was always created as
    INSTRUCTOR (the base serializer ignored the role). Only DEAN or INSTRUCTOR
    can be self-selected — ADMIN is never grantable through public registration.
    """

    role = serializers.ChoiceField(
        choices=[User.Role.DEAN, User.Role.INSTRUCTOR],
        default=User.Role.INSTRUCTOR,
        required=False,
    )
    # The real full name the user typed at signup. Persisted to first/last name
    # so get_full_name() — and therefore every display_name in the app — shows
    # the real name instead of the auto-generated username.
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def get_cleaned_data(self):
        data = super().get_cleaned_data()
        data['role'] = self.validated_data.get('role', User.Role.INSTRUCTOR)
        data['name'] = self.validated_data.get('name', '')
        return data

    def custom_signup(self, request, user):
        changed = []

        role = self.validated_data.get('role') or User.Role.INSTRUCTOR
        if role not in (User.Role.DEAN, User.Role.INSTRUCTOR):
            role = User.Role.INSTRUCTOR
        if user.role != role:
            user.role = role
            changed.append('role')

        name = (self.validated_data.get('name') or '').strip()
        if name:
            first, _, last = name.partition(' ')
            user.first_name = first[:150]
            user.last_name = last.strip()[:150]
            changed.extend(['first_name', 'last_name'])

        if changed:
            user.save(update_fields=changed)


class UserReadSerializer(serializers.ModelSerializer):
    """Read-only representation of a user account.

    Exposes only non-sensitive fields; passwords and permissions are
    intentionally excluded.
    """

    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'role', 'is_active', 'created_at']
        read_only_fields = fields


class UserCreateSerializer(serializers.ModelSerializer):
    """Create a new user account (admin only).

    Password is write-only and hashed via ``create_user()``.
    """

    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ['email', 'username', 'password', 'role']

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class UserUpdateSerializer(serializers.ModelSerializer):
    """Update an existing user's profile fields (admin only).

    Password changes are handled separately by dj-rest-auth.
    """

    class Meta:
        model = User
        fields = ['email', 'username', 'role', 'is_active']


class NotificationSerializer(serializers.ModelSerializer):
    """Read-only representation of an in-app notification for the bell icon.

    For invite-related notifications (those whose ``metadata`` carries an
    ``invite_id``) the *current* :class:`WorkspaceInvite` status is resolved and
    exposed as ``invite_status`` so the bell can show a PENDING / ACCEPTED /
    DECLINED badge that stays accurate even after the invite is responded to.
    """

    invite_status = serializers.SerializerMethodField()
    invite_token = serializers.SerializerMethodField()
    workspace_name = serializers.SerializerMethodField()
    dean_email = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id',
            'notif_type',
            'title',
            'body',
            'is_read',
            'is_dismissed',
            'invite_status',
            'invite_token',
            'workspace_name',
            'dean_email',
            'metadata',
            'created_at',
        ]
        read_only_fields = fields

    def get_invite_status(self, obj):
        invite_id = (obj.metadata or {}).get('invite_id')
        if not invite_id:
            return None
        invite = WorkspaceInvite.objects.filter(pk=invite_id).only('status').first()
        return invite.status if invite else None

    def get_invite_token(self, obj):
        """The secret token for accept/decline, surfaced for invite notifications."""
        return (obj.metadata or {}).get('token')

    def get_workspace_name(self, obj):
        """The workspace this invite targets — null for non-invite notifications."""
        return (obj.metadata or {}).get('workspace_name')

    def get_dean_email(self, obj):
        """Email of the dean who sent the invite — null for non-invite notifications."""
        return (obj.metadata or {}).get('dean_email')


class WorkspaceInviteSerializer(serializers.ModelSerializer):
    """Read-only representation of a workspace invite for the dean's list view."""

    instructor_email = serializers.EmailField(source='instructor.email', read_only=True)
    dean_email = serializers.EmailField(source='dean.email', read_only=True)
    exam_name = serializers.CharField(source='exam.name', read_only=True, default=None)
    workspace_name = serializers.CharField(source='workspace.name', read_only=True, default=None)
    target_name = serializers.CharField(read_only=True)

    class Meta:
        model = WorkspaceInvite
        fields = [
            'id',
            'token',
            'status',
            'instructor',
            'instructor_email',
            'dean',
            'dean_email',
            'workspace',
            'workspace_name',
            'exam',
            'exam_name',
            'target_name',
            'created_at',
            'responded_at',
        ]
        read_only_fields = fields


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    """A single workspace member, denormalised from the membership row."""

    user_id = serializers.UUIDField(source='instructor.id', read_only=True)
    email = serializers.EmailField(source='instructor.email', read_only=True)
    username = serializers.CharField(source='instructor.username', read_only=True)
    display_name = serializers.SerializerMethodField()
    role = serializers.CharField(source='instructor.role', read_only=True)

    class Meta:
        model = WorkspaceMembership
        fields = [
            'id',
            'user_id',
            'email',
            'username',
            'display_name',
            'role',
            'joined_at',
        ]
        read_only_fields = fields

    def get_display_name(self, obj):
        instructor = obj.instructor
        return (instructor.get_full_name() or '').strip() or instructor.username


class WorkspaceSerializer(serializers.ModelSerializer):
    """Read-only workspace with its owner, member count, and member list."""

    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    member_count = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = [
            'id',
            'name',
            'owner',
            'owner_email',
            'member_count',
            'members',
            'created_at',
        ]
        read_only_fields = fields

    def get_member_count(self, obj):
        memberships = getattr(obj, 'memberships', None)
        return memberships.count() if memberships is not None else 0

    def get_members(self, obj):
        memberships = obj.memberships.select_related('instructor').all()
        return WorkspaceMemberSerializer(memberships, many=True).data


class WorkspaceWriteSerializer(serializers.ModelSerializer):
    """Create or rename a workspace — only the name is client-writable."""

    class Meta:
        model = Workspace
        fields = ['name']

    def validate_name(self, value):
        cleaned = (value or '').strip()
        if not cleaned:
            raise serializers.ValidationError('Workspace name is required.')
        return cleaned


class AutoExamSessionReadSerializer(serializers.ModelSerializer):
    """Read-only representation of a scheduled auto recording session."""

    exam_name = serializers.CharField(source='exam.name', read_only=True)
    instructor_email = serializers.EmailField(source='instructor.email', read_only=True)

    class Meta:
        model = AutoExamSession
        fields = [
            'id',
            'exam',
            'exam_name',
            'instructor',
            'instructor_email',
            'scheduled_start',
            'scheduled_end',
            'is_auto',
            'created_at',
        ]
        read_only_fields = fields


class AutoExamSessionCreateSerializer(serializers.ModelSerializer):
    """Create an auto recording session for the requesting instructor.

    ``scheduled_start`` must be in the future and ``scheduled_end`` must be after
    it. An exam is auto-created (or reused) from ``exam_name`` so the captured
    video later groups under the same exam on upload.
    """

    exam_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    exam_id = serializers.UUIDField(write_only=True, required=False)

    class Meta:
        model = AutoExamSession
        fields = ['exam_id', 'exam_name', 'scheduled_start', 'scheduled_end']

    def validate_scheduled_start(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError('Scheduled start must be in the future.')
        return value

    def validate(self, attrs):
        start = attrs.get('scheduled_start')
        end = attrs.get('scheduled_end')
        if start and end and end <= start:
            raise serializers.ValidationError(
                {'scheduled_end': 'End time must be after the start time.'}
            )
        return attrs

    def create(self, validated_data):
        # Local import avoids a circular import (selectors imports models only).
        from .selectors import can_supervise_exam

        request = self.context['request']
        exam_id = validated_data.pop('exam_id', None)
        exam_name = (validated_data.pop('exam_name', '') or '').strip()

        if exam_id:
            # Calendar flow: attach to the existing exam and enforce permission.
            exam = Exam.objects.filter(pk=exam_id).first()
            if exam is None:
                raise serializers.ValidationError({'exam_id': 'Exam not found.'})
            if not can_supervise_exam(request.user, exam):
                raise serializers.ValidationError(
                    {'exam_id': 'Only assigned supervisors or the dean can schedule recording for this exam.'}
                )
        else:
            exam, _created = Exam.objects.get_or_create(
                instructor=request.user,
                name=exam_name or 'Auto Recording',
                defaults={'description': 'Auto-created for a scheduled auto recording.'},
            )

        return AutoExamSession.objects.create(
            exam=exam,
            instructor=request.user,
            scheduled_start=validated_data['scheduled_start'],
            scheduled_end=validated_data['scheduled_end'],
        )


class UserPreferencesReadSerializer(serializers.ModelSerializer):
    """Read-only representation of the authenticated user's preferences.

    The owning user is represented by email only. Clients should not receive
    or submit a writable ``user`` field for preferences.
    """

    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = UserPreferences
        fields = [
            'email_notifications',
            'dashboard_alerts',
            'preferred_language',
            'timezone',
            'theme',
            'metadata',
            'user_email',
            'updated_at',
        ]
        read_only_fields = fields


class UserPreferencesUpdateSerializer(serializers.ModelSerializer):
    """Update preference fields for the authenticated user.

    Ownership is handled in the view through ``request.user``. The ``user``
    field is intentionally not exposed, preventing cross-account updates.
    """

    class Meta:
        model = UserPreferences
        fields = [
            'email_notifications',
            'dashboard_alerts',
            'preferred_language',
            'timezone',
            'theme',
            'metadata',
        ]


class StudentSerializer(serializers.ModelSerializer):
    """Read/write serializer for an instructor's roster entry.

    ``owner`` is taken from the request, never the client. The per-owner
    uniqueness of ``student_id`` is enforced in :meth:`validate_student_id`
    so the API returns a friendly field error instead of a database 500.
    """

    class Meta:
        model = Student
        fields = [
            'id',
            'student_id',
            'full_name',
            'faculty',
            'major',
            'academic_year',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_student_id(self, value):
        """Reject a duplicate student_id within the same owner's roster."""
        request = self.context['request']
        queryset = Student.objects.filter(owner=request.user, student_id=value)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError('A student with this ID already exists.')
        return value

    def create(self, validated_data):
        request = self.context['request']
        return Student.objects.create(owner=request.user, **validated_data)


class ExamReadSerializer(serializers.ModelSerializer):
    """Read-only representation of an exam, including the instructor's email."""

    instructor_email = serializers.EmailField(source='instructor.email', read_only=True)

    class Meta:
        model = Exam
        fields = [
            'id',
            'name',
            'description',
            'instructor',
            'instructor_email',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class ExamCreateSerializer(serializers.ModelSerializer):
    """Create a new exam.

    The ``instructor`` is automatically set to the authenticated user from
    the request context — clients cannot specify it.
    """

    class Meta:
        model = Exam
        fields = ['name', 'description']

    def create(self, validated_data):
        """Persist the exam with the requesting user as instructor."""
        request = self.context['request']
        return Exam.objects.create(instructor=request.user, **validated_data)


class ExamUpdateSerializer(serializers.ModelSerializer):
    """Update an existing exam's name and/or description."""

    class Meta:
        model = Exam
        fields = ['name', 'description']


class ExamSessionReadSerializer(serializers.ModelSerializer):
    """Read-only exam session with denormalised exam name and instructor email."""

    exam_name = serializers.CharField(source='exam.name', read_only=True)
    instructor_email = serializers.EmailField(source='exam.instructor.email', read_only=True)

    class Meta:
        model = ExamSession
        fields = [
            'id',
            'exam',
            'exam_name',
            'instructor_email',
            'student_identifier',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class ExamSessionCreateSerializer(serializers.ModelSerializer):
    """Create a new exam session for a specific student.

    Only the exam's owning instructor (or an admin) may create sessions.
    """

    class Meta:
        model = ExamSession
        fields = ['exam', 'student_identifier']

    def validate_exam(self, exam):
        """Ensure the requesting user owns this exam (or is an admin)."""
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only create sessions for your own exams.')
        return exam


class VideoReadSerializer(serializers.ModelSerializer):
    """Read-only video representation with an absolute ``file_url``.

    Includes denormalised exam name and student identifier for convenience
    so that API consumers don't need a second request.
    """

    file_url = serializers.SerializerMethodField()
    annotated_video_url = serializers.SerializerMethodField()
    analysis = serializers.SerializerMethodField()
    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    student_identifier = serializers.CharField(source='session.student_identifier', read_only=True)
    session_status = serializers.CharField(source='session.status', read_only=True)
    uploaded_by_email = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            'id',
            'session',
            'exam_name',
            'student_identifier',
            'session_status',
            'file_url',
            'annotated_video_url',
            'analysis',
            'original_filename',
            'content_type',
            'size_bytes',
            'file_hash',
            'uploaded_by_email',
            'duration_seconds',
            'uploaded_at',
            'expires_at',
        ]
        read_only_fields = fields

    def get_file_url(self, obj):
        """Return an absolute URL to the stored video file.

        Falls back to a relative URL when no request is available in the
        serializer context (e.g. during management commands).
        """
        if not obj.file:
            return ''

        request = self.context.get('request')
        if request is None:
            return obj.file.url
        return request.build_absolute_uri(obj.file.url)

    def get_annotated_video_url(self, obj):
        """Absolute URL to the annotated analysis video, or ``None``.

        The pipeline writes a full-length copy of the video with detection
        boxes/labels drawn on flagged frames and stores its ``/media/...`` URL
        on the session's :class:`AnalysisJob` metadata (H8). Returns ``None``
        until analysis has produced one.
        """
        job = getattr(obj.session, 'analysis_job', None)
        url = (job.metadata or {}).get('annotated_video_url') if job else None
        if not url:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request is not None else url

    def get_analysis(self, obj):
        """Include the session report when analysis has completed."""
        if not hasattr(obj.session, 'report'):
            return None
        return ReportReadSerializer(obj.session.report).data

    def get_uploaded_by_email(self, obj):
        """Prefer the real uploader; fall back to the exam owner for older rows."""
        if obj.uploaded_by_id:
            return obj.uploaded_by.email
        instructor = obj.session.exam.instructor
        return instructor.email if instructor else None


class VideoUploadSerializer(serializers.ModelSerializer):
    """Handle video file uploads with format validation and deduplication.

    Validation steps:

    1. **Ownership** — only the session's instructor (or admin) may upload.
    2. **Format** — both the MIME type and file extension are checked.
    3. **Deduplication (FR4)** — a SHA-256 hash is computed over the entire
       file content; if the hash already exists the upload is rejected.

    On success the serializer auto-populates ``original_filename``,
    ``content_type``, ``size_bytes``, and ``file_hash``.
    """

    class Meta:
        model = Video
        fields = ['session', 'file', 'duration_seconds']
        extra_kwargs = {
            # `session` is optional at the data layer: the view resolves (or
            # auto-creates) one and passes it to ``save(session=...)``. This lets
            # the view hand ``request.data`` straight to the serializer without
            # copying the QueryDict — copying deep-copies the uploaded file and
            # raises "cannot pickle 'BufferedRandom' instances".
            'session': {'required': False, 'allow_null': True},
            'duration_seconds': {'required': False, 'allow_null': True},
        }

    def validate_session(self, session):
        """Ensure the requesting user owns this session's exam."""
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and session.exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only upload videos for your own exam sessions.')
        return session

    # Recognised video container extensions (allowlist).
    ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}
    # Top-level ISO Base Media / QuickTime atom types that legitimately begin
    # an mp4/mov/m4v file (the type sits at byte offset 4). Modern files start
    # with 'ftyp'; legacy/streamed QuickTime may lead with another atom.
    _ISO_BMFF_ATOMS = {b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide', b'pnot'}

    def validate_file(self, file):
        """Reject anything that is not a plausible video within the size limit.

        Three allowlist gates (N2/N6) — a positive match is required to pass,
        rather than the previous "reject a few known-bad shapes" approach:

        1. **Size** — empty files and anything larger than
           ``settings.MAX_UPLOAD_SIZE_BYTES`` are rejected (mirrors the Nginx
           ``client_max_body_size``).
        2. **Extension** — must be one of :attr:`ALLOWED_VIDEO_EXTENSIONS`.
        3. **Content sniff** — the file header must match a known container
           signature. The ``Content-Type`` header is advisory and trivially
           spoofed, so it is no longer trusted: previously a non-video renamed
           to ``clip.mp4`` with no ``Content-Type`` sailed through to OpenCV.
        """
        max_size = getattr(settings, 'MAX_UPLOAD_SIZE_BYTES', 1024 * 1024 * 1024)
        size = getattr(file, 'size', 0) or 0
        if size <= 0:
            raise serializers.ValidationError('The uploaded file is empty.')
        if size > max_size:
            raise serializers.ValidationError(
                f'Video exceeds the maximum upload size of {max_size // (1024 * 1024)} MB.'
            )

        extension = Path(getattr(file, 'name', '')).suffix.lower()
        if extension not in self.ALLOWED_VIDEO_EXTENSIONS:
            raise serializers.ValidationError('Supported video formats: mp4, mov, m4v, webm, avi, mkv.')

        if not self._looks_like_video(file):
            raise serializers.ValidationError('Upload a valid video file.')

        return file

    @classmethod
    def _looks_like_video(cls, file):
        """Return True if the file header matches a known video container.

        Reads the first 16 bytes and seeks back to 0 so the stream stays intact
        for hashing/persistence. Recognises ISO-BMFF / QuickTime (mp4, mov,
        m4v), Matroska / WebM (EBML magic), and RIFF/AVI.
        """
        try:
            file.seek(0)
            header = file.read(16)
        finally:
            file.seek(0)

        if len(header) < 12:
            return False
        # ISO Base Media / QuickTime: atom type at bytes 4..8.
        if header[4:8] in cls._ISO_BMFF_ATOMS:
            return True
        # Matroska / WebM: EBML magic number.
        if header[:4] == b'\x1a\x45\xdf\xa3':
            return True
        # RIFF AVI: 'RIFF' .... 'AVI '.
        if header[:4] == b'RIFF' and header[8:12] == b'AVI ':
            return True
        return False

    def create(self, validated_data):
        """Compute the file hash, reject a per-session duplicate, then persist.

        The same file may be uploaded for *different* sessions, but never twice
        for the *same* session (FR4 + the per-session rule).
        """
        file = validated_data['file']
        digest = hashlib.sha256()
        for chunk in file.chunks():
            digest.update(chunk)
        file.seek(0)

        file_hash = digest.hexdigest()
        session = validated_data['session']
        if Video.objects.filter(session=session, file_hash=file_hash).exists():
            raise serializers.ValidationError(
                {'file': 'This video has already been uploaded for this session.'}
            )

        request = self.context.get('request')
        return Video.objects.create(
            **validated_data,
            original_filename=getattr(file, 'name', '') or 'upload',
            content_type=getattr(file, 'content_type', '') or '',
            size_bytes=getattr(file, 'size', 0) or 0,
            file_hash=file_hash,
            uploaded_by=request.user if request is not None else None,
            expires_at=timezone.now() + timedelta(days=30),
        )


class AlertReadSerializer(serializers.ModelSerializer):
    """Read-only alert with denormalised exam name and reviewer email.

    ``reviewed_by_email`` defaults to ``None`` when the alert has not yet
    been reviewed (the FK is nullable).
    """

    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    reviewed_by_email = serializers.EmailField(
        source='reviewed_by.email', read_only=True, default=None,
    )

    class Meta:
        model = Alert
        fields = [
            'id',
            'session',
            'exam_name',
            'timestamp_sec',
            'behavior_type',
            'severity',
            'confidence_score',
            'metadata',
            'snapshot_url',
            'clip_url',
            'is_reviewed',
            'reviewed_at',
            'reviewed_by',
            'reviewed_by_email',
            'created_at',
        ]
        read_only_fields = fields


class AlertCreateSerializer(serializers.ModelSerializer):
    """Create a new alert for an exam session.

    Typically called by the AI pipeline after detecting a suspicious
    behaviour in a video frame.  Ownership validation ensures only the
    session's instructor (or an admin) can write alerts.
    """

    class Meta:
        model = Alert
        fields = ['session', 'timestamp_sec', 'behavior_type', 'severity', 'confidence_score', 'metadata', 'snapshot_url']

    def validate_session(self, session):
        """Ensure the requesting user owns this session's exam."""
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and session.exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only create alerts for your own exam sessions.')
        return session


class AlertReviewSerializer(serializers.ModelSerializer):
    """Mark an alert as reviewed or revert it to unreviewed.

    When ``is_reviewed`` is ``True`` the alert's ``mark_reviewed()`` model
    method is called.  When ``False`` the review fields are cleared.
    """

    class Meta:
        model = Alert
        fields = ['is_reviewed']

    def update(self, instance, validated_data):
        """Toggle the review state and persist."""
        request = self.context['request']
        if validated_data.get('is_reviewed'):
            instance.mark_reviewed(request.user)
            return instance

        instance.is_reviewed = False
        instance.reviewed_by = None
        instance.reviewed_at = None
        instance.save(update_fields=['is_reviewed', 'reviewed_by', 'reviewed_at'])
        return instance


class AuditLogReadSerializer(serializers.ModelSerializer):
    """Read-only audit log entry.

    ``user_email`` defaults to ``None`` when the acting user has been deleted
    (the FK uses ``SET_NULL``).
    """

    user_email = serializers.EmailField(source='user.email', read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'user',
            'user_email',
            'action',
            'target_resource',
            'ip_address',
            'user_agent',
            'metadata',
            'performed_at',
        ]
        read_only_fields = fields


class SystemSettingsReadSerializer(serializers.ModelSerializer):
    """Read-only system setting with the last modifier's email."""

    updated_by_email = serializers.EmailField(source='updated_by.email', read_only=True, default=None)

    class Meta:
        model = SystemSettings
        fields = [
            'setting_key',
            'setting_value',
            'description',
            'updated_by',
            'updated_by_email',
            'updated_at',
        ]
        read_only_fields = fields


class SystemSettingsUpdateSerializer(serializers.ModelSerializer):
    """Update a system setting's value and/or description.

    Automatically records the requesting user as ``updated_by``.
    """

    class Meta:
        model = SystemSettings
        fields = ['setting_value', 'description']

    def update(self, instance, validated_data):
        """Apply changes and stamp the current user as the modifier."""
        request = self.context['request']
        instance.setting_value = validated_data.get('setting_value', instance.setting_value)
        instance.description = validated_data.get('description', instance.description)
        instance.updated_by = request.user
        instance.save(update_fields=['setting_value', 'description', 'updated_by', 'updated_at'])
        return instance


class AnalysisJobReadSerializer(serializers.ModelSerializer):
    """Read-only analysis job with denormalised exam/student info."""

    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    student_identifier = serializers.CharField(source='session.student_identifier', read_only=True)

    class Meta:
        model = AnalysisJob
        fields = [
            'id',
            'session',
            'exam_name',
            'student_identifier',
            'status',
            'ai_model_version',
            'frame_sample_rate',
            'started_at',
            'completed_at',
            'error_message',
            'metadata',
            'created_at',
        ]
        read_only_fields = fields


class AnalysisJobCreateSerializer(serializers.ModelSerializer):
    """Queue a new AI analysis job for an exam session.

    Ownership validation ensures only the session's instructor (or admin)
    can trigger analysis.
    """

    class Meta:
        model = AnalysisJob
        # ai_model_version is not a client input — the pipeline stamps it with
        # the model that actually ran (H3). frame_sample_rate is client-tunable
        # and wired into the pipeline (H2).
        fields = ['session', 'frame_sample_rate']

    def validate_session(self, session):
        """Ensure the requesting user owns this session's exam."""
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and session.exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only create analysis jobs for your own exam sessions.')
        return session


class ReportReadSerializer(serializers.ModelSerializer):
    """Read-only analysis report with denormalised exam/student info."""

    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    student_identifier = serializers.CharField(source='session.student_identifier', read_only=True)

    class Meta:
        model = Report
        fields = [
            'id',
            'session',
            'exam_name',
            'student_identifier',
            'overall_cheating_probability',
            'total_alerts',
            'alerts_by_type',
            'processing_time_seconds',
            'summary',
            'generated_at',
        ]
        read_only_fields = fields


class ReportCreateSerializer(serializers.ModelSerializer):
    """Create an analysis report for a completed exam session.

    Typically called by the AI pipeline once analysis finishes.
    Ownership validation ensures only the session's instructor (or admin)
    can write reports.
    """

    class Meta:
        model = Report
        fields = [
            'session',
            'overall_cheating_probability',
            'total_alerts',
            'alerts_by_type',
            'processing_time_seconds',
            'summary',
        ]

    def validate_session(self, session):
        """Ensure the requesting user owns this session's exam."""
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and session.exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only create reports for your own exam sessions.')
        return session


# Backwards-compatible names for the current simple views.
UserSerializer = UserReadSerializer
UserPreferencesSerializer = UserPreferencesReadSerializer
ExamSessionSerializer = ExamSessionReadSerializer
VideoSerializer = VideoReadSerializer
AlertSerializer = AlertReadSerializer
AuditLogSerializer = AuditLogReadSerializer
SystemSettingsSerializer = SystemSettingsReadSerializer
AnalysisJobSerializer = AnalysisJobReadSerializer
ReportSerializer = ReportReadSerializer

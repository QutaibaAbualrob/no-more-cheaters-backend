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

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Alert, AnalysisJob, AuditLog, Exam, ExamSession, Report,
    Student, SystemSettings, UserPreferences, Video,
)


User = get_user_model()


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
    analysis = serializers.SerializerMethodField()
    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    student_identifier = serializers.CharField(source='session.student_identifier', read_only=True)
    session_status = serializers.CharField(source='session.status', read_only=True)
    uploaded_by_email = serializers.EmailField(source='session.exam.instructor.email', read_only=True)

    class Meta:
        model = Video
        fields = [
            'id',
            'session',
            'exam_name',
            'student_identifier',
            'session_status',
            'file_url',
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

    def get_analysis(self, obj):
        """Include the session report when analysis has completed."""
        if not hasattr(obj.session, 'report'):
            return None
        return ReportReadSerializer(obj.session.report).data


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

    def validate_file(self, file):
        """Reject files that are not a recognised video format.

        Checks both the ``Content-Type`` header and the file extension.
        Allowed formats: mp4, mov, m4v, webm, avi, mkv.
        """
        content_type = getattr(file, 'content_type', '') or ''
        extension = Path(getattr(file, 'name', '')).suffix.lower()
        allowed_extensions = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}

        if content_type and not content_type.startswith('video/') and content_type != 'application/octet-stream':
            raise serializers.ValidationError('Upload a valid video file.')

        if extension not in allowed_extensions:
            raise serializers.ValidationError('Supported video formats: mp4, mov, m4v, webm, avi, mkv.')

        return file

    def create(self, validated_data):
        """Compute file hash, check for duplicates, then persist the Video.

        Raises :class:`~rest_framework.serializers.ValidationError` if a
        video with identical content has already been uploaded (FR4).
        """
        file = validated_data['file']
        digest = hashlib.sha256()
        for chunk in file.chunks():
            digest.update(chunk)
        file.seek(0)

        file_hash = digest.hexdigest()
        if Video.objects.filter(file_hash=file_hash).exists():
            raise serializers.ValidationError(
                {'file': 'This video has already been uploaded (duplicate content).'}
            )

        return Video.objects.create(
            **validated_data,
            original_filename=getattr(file, 'name', '') or 'upload',
            content_type=getattr(file, 'content_type', '') or '',
            size_bytes=getattr(file, 'size', 0) or 0,
            file_hash=file_hash,
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
        fields = ['session', 'ai_model_version', 'frame_sample_rate']

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

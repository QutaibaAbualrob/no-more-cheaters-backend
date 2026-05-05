import hashlib
from pathlib import Path

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Alert, AuditLog, Exam, ExamSession, SystemSettings, Video


User = get_user_model()


class UserReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'role', 'is_active', 'created_at']
        read_only_fields = fields


class ExamReadSerializer(serializers.ModelSerializer):
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
    class Meta:
        model = Exam
        fields = ['name', 'description']

    def create(self, validated_data):
        request = self.context['request']
        return Exam.objects.create(instructor=request.user, **validated_data)


class ExamSessionReadSerializer(serializers.ModelSerializer):
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
    class Meta:
        model = ExamSession
        fields = ['exam', 'student_identifier']

    def validate_exam(self, exam):
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only create sessions for your own exams.')
        return exam


class VideoReadSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    student_identifier = serializers.CharField(source='session.student_identifier', read_only=True)

    class Meta:
        model = Video
        fields = [
            'id',
            'session',
            'exam_name',
            'student_identifier',
            'file_url',
            'original_filename',
            'content_type',
            'size_bytes',
            'file_hash',
            'duration_seconds',
            'uploaded_at',
        ]
        read_only_fields = fields

    def get_file_url(self, obj):
        if not obj.file:
            return ''

        request = self.context.get('request')
        if request is None:
            return obj.file.url
        return request.build_absolute_uri(obj.file.url)


class VideoUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Video
        fields = ['session', 'file', 'duration_seconds']
        extra_kwargs = {
            'duration_seconds': {'required': False, 'allow_null': True},
        }

    def validate_session(self, session):
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and session.exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only upload videos for your own exam sessions.')
        return session

    def validate_file(self, file):
        content_type = getattr(file, 'content_type', '') or ''
        extension = Path(getattr(file, 'name', '')).suffix.lower()
        allowed_extensions = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}

        if content_type and not content_type.startswith('video/') and content_type != 'application/octet-stream':
            raise serializers.ValidationError('Upload a valid video file.')

        if extension not in allowed_extensions:
            raise serializers.ValidationError('Supported video formats: mp4, mov, m4v, webm, avi, mkv.')

        return file

    def create(self, validated_data):
        file = validated_data['file']
        digest = hashlib.sha256()
        for chunk in file.chunks():
            digest.update(chunk)
        file.seek(0)

        return Video.objects.create(
            **validated_data,
            original_filename=getattr(file, 'name', '') or 'upload',
            content_type=getattr(file, 'content_type', '') or '',
            size_bytes=getattr(file, 'size', 0) or 0,
            file_hash=digest.hexdigest(),
        )


class AlertReadSerializer(serializers.ModelSerializer):
    exam_name = serializers.CharField(source='session.exam.name', read_only=True)
    reviewed_by_email = serializers.EmailField(source='reviewed_by.email', read_only=True)

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
            'is_reviewed',
            'reviewed_at',
            'reviewed_by',
            'reviewed_by_email',
            'created_at',
        ]
        read_only_fields = fields


class AlertCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ['session', 'timestamp_sec', 'behavior_type', 'severity', 'confidence_score', 'metadata']

    def validate_session(self, session):
        request = self.context['request']
        is_admin = getattr(request.user, 'role', '') == User.Role.ADMIN or request.user.is_superuser
        if not is_admin and session.exam.instructor_id != request.user.id:
            raise serializers.ValidationError('You can only create alerts for your own exam sessions.')
        return session


class AlertReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ['is_reviewed']

    def update(self, instance, validated_data):
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
    user_email = serializers.EmailField(source='user.email', read_only=True)

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
    updated_by_email = serializers.EmailField(source='updated_by.email', read_only=True)

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
    class Meta:
        model = SystemSettings
        fields = ['setting_value', 'description']

    def update(self, instance, validated_data):
        request = self.context['request']
        instance.setting_value = validated_data.get('setting_value', instance.setting_value)
        instance.description = validated_data.get('description', instance.description)
        instance.updated_by = request.user
        instance.save(update_fields=['setting_value', 'description', 'updated_by', 'updated_at'])
        return instance


# Backwards-compatible names for the current simple views.
UserSerializer = UserReadSerializer
ExamSessionSerializer = ExamSessionReadSerializer
VideoSerializer = VideoReadSerializer
AlertSerializer = AlertReadSerializer
AuditLogSerializer = AuditLogReadSerializer
SystemSettingsSerializer = SystemSettingsReadSerializer

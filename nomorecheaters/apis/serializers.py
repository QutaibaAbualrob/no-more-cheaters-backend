
# from rest_framework import serializers
# from django.contrib.auth.models import User

# class UserSerializer(serializers.ModelSerializer):            --------> Past
#     class Meta:
#         model = User
#         fields = ['id', 'username', 'email', 'password']


from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import ExamSession, Video, Alert, AuditLog, SystemSettings

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model"""
    
    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'role', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class ExamSessionSerializer(serializers.ModelSerializer):
    """Serializer for ExamSession model"""
    
    instructor_email = serializers.EmailField(source='instructor.email', read_only=True)
    instructor_name = serializers.CharField(source='instructor.username', read_only=True)
    
    class Meta:
        model = ExamSession
        fields = [
            'id', 'instructor', 'instructor_email', 'instructor_name',
            'student_identifier', 'exam_name', 'status', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class VideoSerializer(serializers.ModelSerializer):
    """Serializer for Video model"""
    
    session_exam_name = serializers.CharField(source='session.exam_name', read_only=True)
    session_student = serializers.CharField(source='session.student_identifier', read_only=True)
    
    class Meta:
        model = Video
        fields = [
            'id', 'session', 'session_exam_name', 'session_student',
            'file_url', 'file_hash', 'duration_seconds', 'uploaded_at'
        ]
        read_only_fields = ['id', 'uploaded_at']


class AlertSerializer(serializers.ModelSerializer):
    """Serializer for Alert model"""
    
    session_exam_name = serializers.CharField(source='session.exam_name', read_only=True)
    reviewed_by_email = serializers.EmailField(source='reviewed_by.email', read_only=True)
    
    class Meta:
        model = Alert
        fields = [
            'id', 'session', 'session_exam_name', 'timestamp_sec',
            'behavior_type', 'confidence_score', 'is_reviewed',
            'reviewed_at', 'reviewed_by', 'reviewed_by_email', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class AuditLogSerializer(serializers.ModelSerializer):
    """Serializer for AuditLog model"""
    
    user_email = serializers.EmailField(source='user.email', read_only=True)
    
    class Meta:
        model = AuditLog
        fields = [
            'id', 'user', 'user_email', 'action', 'target_resource',
            'ip_address', 'user_agent', 'performed_at'
        ]
        read_only_fields = ['id', 'performed_at']


class SystemSettingsSerializer(serializers.ModelSerializer):
    """Serializer for SystemSettings model"""
    
    updated_by_email = serializers.EmailField(source='updated_by.email', read_only=True)
    
    class Meta:
        model = SystemSettings
        fields = [
            'setting_key', 'setting_value', 'description',
            'updated_by', 'updated_by_email', 'updated_at'
        ]
        read_only_fields = ['updated_at']
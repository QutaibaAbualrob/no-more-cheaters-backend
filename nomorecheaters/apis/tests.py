import hashlib
import shutil
import tempfile
import uuid
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Alert, Exam, ExamSession, SystemSettings, Video
from .serializers import (
    AlertReviewSerializer,
    ExamCreateSerializer,
    ExamSessionCreateSerializer,
    SystemSettingsUpdateSerializer,
    VideoUploadSerializer,
)


User = get_user_model()


def make_user(*, email='instructor@example.com', username='instructor', role=None):
    suffix = uuid.uuid4().hex[:8]
    if email == 'instructor@example.com':
        email = f'instructor-{suffix}@example.com'
    if username == 'instructor':
        username = f'instructor-{suffix}'

    return User.objects.create_user(
        username=username,
        email=email,
        password='test-password-123',
        role=role or User.Role.INSTRUCTOR,
    )


def make_exam(*, instructor=None, name='Midterm Exam'):
    return Exam.objects.create(
        instructor=instructor or make_user(),
        name=name,
        description='Automated test exam.',
    )


def make_session(*, exam=None, student_identifier='student-001'):
    return ExamSession.objects.create(
        exam=exam or make_exam(),
        student_identifier=student_identifier,
    )


def request_for(user):
    return SimpleNamespace(user=user)


class UserModelTests(TestCase):
    def test_user_primary_key_is_uuid(self):
        user = make_user()

        self.assertIsInstance(user.id, uuid.UUID)
        self.assertEqual(user.pk, user.id)


class ExamSessionModelTests(TestCase):
    def test_student_identifier_is_unique_per_exam(self):
        exam = make_exam()
        make_session(exam=exam, student_identifier='student-001')

        with self.assertRaises(IntegrityError):
            make_session(exam=exam, student_identifier='student-001')

    def test_same_student_identifier_can_exist_in_different_exams(self):
        instructor = make_user()
        first_exam = make_exam(instructor=instructor, name='First Exam')
        second_exam = make_exam(instructor=instructor, name='Second Exam')

        make_session(exam=first_exam, student_identifier='student-001')
        make_session(exam=second_exam, student_identifier='student-001')

        self.assertEqual(ExamSession.objects.count(), 2)


class AlertModelTests(TestCase):
    def test_confidence_score_must_be_between_zero_and_one(self):
        session = make_session()
        alert = Alert(
            session=session,
            timestamp_sec=10,
            behavior_type=Alert.BehaviorType.PHONE_DETECTED,
            confidence_score=1.5,
        )

        with self.assertRaises(ValidationError):
            alert.full_clean()

    def test_timestamp_cannot_be_negative(self):
        session = make_session()
        alert = Alert(
            session=session,
            timestamp_sec=-1,
            behavior_type=Alert.BehaviorType.LOOKING_AWAY,
            confidence_score=0.5,
        )

        with self.assertRaises(ValidationError):
            alert.full_clean()

    def test_mark_reviewed_sets_review_fields(self):
        reviewer = make_user(email='reviewer@example.com', username='reviewer')
        alert = Alert.objects.create(
            session=make_session(),
            timestamp_sec=30,
            behavior_type=Alert.BehaviorType.MULTIPLE_FACES,
            confidence_score=0.75,
        )

        alert.mark_reviewed(reviewer)
        alert.refresh_from_db()

        self.assertTrue(alert.is_reviewed)
        self.assertEqual(alert.reviewed_by, reviewer)
        self.assertIsNotNone(alert.reviewed_at)


class ExamSerializerTests(TestCase):
    def test_exam_create_serializer_sets_instructor_from_request_user(self):
        instructor = make_user()
        serializer = ExamCreateSerializer(
            data={'name': 'Final Exam', 'description': 'Worth 40%'},
            context={'request': request_for(instructor)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        exam = serializer.save()

        self.assertEqual(exam.instructor, instructor)


class ExamSessionSerializerTests(TestCase):
    def test_instructor_cannot_create_session_for_another_instructors_exam(self):
        owner = make_user(email='owner@example.com', username='owner')
        other = make_user(email='other@example.com', username='other')
        exam = make_exam(instructor=owner)
        serializer = ExamSessionCreateSerializer(
            data={'exam': str(exam.id), 'student_identifier': 'student-001'},
            context={'request': request_for(other)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('exam', serializer.errors)

    def test_admin_can_create_session_for_any_exam(self):
        owner = make_user(email='owner@example.com', username='owner')
        admin_user = make_user(email='admin@example.com', username='admin', role=User.Role.ADMIN)
        exam = make_exam(instructor=owner)
        serializer = ExamSessionCreateSerializer(
            data={'exam': str(exam.id), 'student_identifier': 'student-001'},
            context={'request': request_for(admin_user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class VideoUploadSerializerTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_video_upload_serializer_populates_file_metadata(self):
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        content = b'fake video content'
        upload = SimpleUploadedFile('exam.mp4', content, content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload, 'duration_seconds': 42},
            context={'request': request_for(instructor)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        video = serializer.save()

        self.assertEqual(video.original_filename, 'exam.mp4')
        self.assertEqual(video.content_type, 'video/mp4')
        self.assertEqual(video.size_bytes, len(content))
        self.assertEqual(video.duration_seconds, 42)
        self.assertEqual(video.file_hash, hashlib.sha256(content).hexdigest())

    def test_video_upload_serializer_rejects_non_video_file(self):
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        upload = SimpleUploadedFile('notes.txt', b'not video', content_type='text/plain')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(instructor)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('file', serializer.errors)

    def test_instructor_cannot_upload_video_for_another_instructors_session(self):
        owner = make_user(email='owner@example.com', username='owner')
        other = make_user(email='other@example.com', username='other')
        session = make_session(exam=make_exam(instructor=owner))
        upload = SimpleUploadedFile('exam.mp4', b'fake video', content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(other)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('session', serializer.errors)

    def test_negative_duration_is_rejected(self):
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        upload = SimpleUploadedFile('exam.mp4', b'fake video', content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload, 'duration_seconds': -1},
            context={'request': request_for(instructor)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('duration_seconds', serializer.errors)


class AlertSerializerTests(TestCase):
    def test_alert_review_serializer_reviews_alert(self):
        reviewer = make_user()
        alert = Alert.objects.create(
            session=make_session(),
            timestamp_sec=20,
            behavior_type=Alert.BehaviorType.OTHER_PERSON,
            confidence_score=0.8,
        )
        serializer = AlertReviewSerializer(
            instance=alert,
            data={'is_reviewed': True},
            context={'request': request_for(reviewer)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        reviewed_alert = serializer.save()

        self.assertTrue(reviewed_alert.is_reviewed)
        self.assertEqual(reviewed_alert.reviewed_by, reviewer)
        self.assertIsNotNone(reviewed_alert.reviewed_at)

    def test_alert_review_serializer_can_unreview_alert(self):
        reviewer = make_user()
        alert = Alert.objects.create(
            session=make_session(),
            timestamp_sec=20,
            behavior_type=Alert.BehaviorType.OTHER_PERSON,
            confidence_score=0.8,
        )
        alert.mark_reviewed(reviewer)

        serializer = AlertReviewSerializer(
            instance=alert,
            data={'is_reviewed': False},
            context={'request': request_for(reviewer)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        unreviewed_alert = serializer.save()

        self.assertFalse(unreviewed_alert.is_reviewed)
        self.assertIsNone(unreviewed_alert.reviewed_by)
        self.assertIsNone(unreviewed_alert.reviewed_at)


class SystemSettingsSerializerTests(TestCase):
    def test_system_settings_update_serializer_sets_updated_by(self):
        updater = make_user()
        setting = SystemSettings.objects.create(
            setting_key='alert_threshold',
            setting_value='0.75',
            description='Old threshold',
            updated_by=updater,
        )
        serializer = SystemSettingsUpdateSerializer(
            instance=setting,
            data={'setting_value': '0.85', 'description': 'New threshold'},
            context={'request': request_for(updater)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()

        self.assertEqual(updated.setting_value, '0.85')
        self.assertEqual(updated.description, 'New threshold')
        self.assertEqual(updated.updated_by, updater)


class AdminRegistrationTests(TestCase):
    def test_core_models_are_registered_in_admin(self):
        for model in [User, Exam, ExamSession, Video, Alert, SystemSettings]:
            self.assertIn(model, admin.site._registry)


class UserAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user(username='testuser', email='testuser@example.com')

    def test_users_list_requires_authentication(self):
        response = self.client.get(reverse('users_list'))

        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_users_list_returns_uuid_id_for_authenticated_user(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse('users_list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(response.data[0]['id'], str(self.user.id))

    def test_user_detail_uses_uuid_route(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse('delete_user', kwargs={'pk': self.user.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], str(self.user.id))

    def test_integer_user_detail_route_does_not_match_uuid_url(self):
        self.client.force_authenticate(self.user)

        response = self.client.get('/api/1/')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

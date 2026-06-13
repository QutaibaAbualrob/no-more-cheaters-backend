"""
Unit and integration tests for the No More Cheaters API layer.

Test organisation
-----------------
* **Model tests** — validate constraints, defaults, and business-logic methods
  directly on the Django ORM models.
* **Serializer tests** — exercise validation rules, authorisation checks, and
  the ``create`` / ``update`` hooks of each DRF serializer.
* **Admin tests** — confirm that every model is registered in the Django admin.
* **API tests** — hit real HTTP endpoints via DRF’s ``APITestCase`` to verify
  routing, authentication, and response payloads.

All tests use an in-memory SQLite database created by Django’s test runner;
no external services are required.
"""

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import (
    Alert, AnalysisJob, AuditLog, Exam, ExamSession, Report,
    SystemSettings, UserPreferences, Video,
)
from .serializers import (
    AlertCreateSerializer,
    AlertReviewSerializer,
    ExamCreateSerializer,
    ExamSessionCreateSerializer,
    ReportCreateSerializer,
    SystemSettingsUpdateSerializer,
    UserPreferencesReadSerializer,
    UserPreferencesUpdateSerializer,
    VideoUploadSerializer,
)
from .tasks import run_analysis


User = get_user_model()


def fake_analysis_result(behavior_type=Alert.BehaviorType.LOOKING_AWAY):
    """Build a deterministic stand-in for :func:`apis.ai.analyze_video`.

    Lets the queue/lifecycle tests exercise the real ``build_ai_report`` path
    without OpenCV, model weights, or a genuine video file. Shaped to duck-type
    an ``AnalysisResult`` with a single consolidated event.
    """
    event = SimpleNamespace(
        behavior_type=behavior_type,
        confidence=0.72,
        start_sec=30.0,
        end_sec=33.0,
        duration_sec=3.0,
        frame_count=3,
        timestamp_sec=30,
    )
    return SimpleNamespace(
        events=[event],
        annotated_video_path=None,
        metadata={
            'events_by_type': {str(behavior_type): 1},
            'total_events': 1,
            'frames_analyzed': 10,
            'processing_time_seconds': 0.5,
        },
    )


def make_user(*, email='instructor@example.com', username='instructor', role=None):
    """Create a test user with a unique email/username suffix.

    When called with the default ``email`` or ``username`` a random 8-char
    hex suffix is appended to avoid ``UNIQUE`` constraint violations across
    tests.
    """
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
    """Create a test exam, generating a default instructor if not provided."""
    return Exam.objects.create(
        instructor=instructor or make_user(),
        name=name,
        description='Automated test exam.',
    )


def make_session(*, exam=None, student_identifier='student-001'):
    """Create a test exam session, generating a default exam if not provided."""
    return ExamSession.objects.create(
        exam=exam or make_exam(),
        student_identifier=student_identifier,
    )


def request_for(user):
    """Return a minimal request-like object carrying *user* for serializer context."""
    return SimpleNamespace(user=user)


class UserModelTests(TestCase):
    """Verify the custom User model uses UUID primary keys."""

    def test_user_primary_key_is_uuid(self):
        user = make_user()

        self.assertIsInstance(user.id, uuid.UUID)
        self.assertEqual(user.pk, user.id)


class UserPreferencesModelTests(TestCase):
    """Verify default preference values for new preference rows."""

    def test_user_preferences_defaults_are_sensible(self):
        user = make_user()
        preferences = UserPreferences.objects.create(user=user)

        self.assertTrue(preferences.email_notifications)
        self.assertTrue(preferences.dashboard_alerts)
        self.assertEqual(preferences.preferred_language, 'en')
        self.assertEqual(preferences.timezone, 'UTC')
        self.assertEqual(preferences.theme, UserPreferences.Theme.SYSTEM)
        self.assertEqual(preferences.metadata, {})


class ExamSessionModelTests(TestCase):
    """Verify the unique-student-per-exam constraint on ExamSession."""

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
    """Validate Alert field constraints and the mark_reviewed business logic."""

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
    """Verify ExamCreateSerializer auto-assigns the instructor from the request."""

    def test_exam_create_serializer_sets_instructor_from_request_user(self):
        instructor = make_user()
        serializer = ExamCreateSerializer(
            data={'name': 'Final Exam', 'description': 'Worth 40%'},
            context={'request': request_for(instructor)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        exam = serializer.save()

        self.assertEqual(exam.instructor, instructor)


class UserPreferencesSerializerTests(TestCase):
    """Verify preference serializers expose safe fields and update values."""

    def test_read_serializer_does_not_expose_writable_user_id(self):
        user = make_user()
        preferences = UserPreferences.objects.create(user=user)

        data = UserPreferencesReadSerializer(preferences).data

        self.assertNotIn('user', data)
        self.assertEqual(data['user_email'], user.email)

    def test_update_serializer_changes_preferences_without_changing_owner(self):
        owner = make_user()
        other = make_user()
        preferences = UserPreferences.objects.create(user=owner)
        serializer = UserPreferencesUpdateSerializer(
            instance=preferences,
            data={
                'email_notifications': False,
                'dashboard_alerts': False,
                'preferred_language': 'ar',
                'timezone': 'Asia/Jerusalem',
                'theme': UserPreferences.Theme.DARK,
                'metadata': {'compact_dashboard': True},
                'user': str(other.id),
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()

        self.assertEqual(updated.user, owner)
        self.assertFalse(updated.email_notifications)
        self.assertFalse(updated.dashboard_alerts)
        self.assertEqual(updated.preferred_language, 'ar')
        self.assertEqual(updated.timezone, 'Asia/Jerusalem')
        self.assertEqual(updated.theme, UserPreferences.Theme.DARK)
        self.assertEqual(updated.metadata, {'compact_dashboard': True})


class ExamSessionSerializerTests(TestCase):
    """Verify ownership and admin-bypass rules on ExamSessionCreateSerializer."""

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
    """Verify video upload validation: metadata, format, ownership, and duration."""

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
    """Verify the review and un-review workflow via AlertReviewSerializer."""

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
    """Verify SystemSettingsUpdateSerializer records the modifier."""

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
    """Ensure every domain model is registered in the Django admin site."""

    def test_core_models_are_registered_in_admin(self):
        for model in [User, Exam, ExamSession, Video, Alert, AuditLog,
                      SystemSettings, UserPreferences, AnalysisJob, Report]:
            self.assertIn(model, admin.site._registry)


class UserAPITests(APITestCase):
    """Integration tests for the User list and detail API endpoints.

    Verifies authentication requirements, UUID-based routing, and that
    integer IDs do not accidentally match UUID URL patterns.
    """

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


class MyPreferencesAPITests(APITestCase):
    """Integration tests for the authenticated user's preferences endpoint."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user(username='prefsuser', email='prefs@example.com')

    def test_preferences_endpoint_requires_authentication(self):
        response = self.client.get(reverse('my_preferences'))

        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_get_preferences_creates_defaults_for_authenticated_user(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse('my_preferences'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(UserPreferences.objects.filter(user=self.user).count(), 1)
        self.assertEqual(response.data['user_email'], self.user.email)
        self.assertTrue(response.data['email_notifications'])
        self.assertEqual(response.data['theme'], UserPreferences.Theme.SYSTEM)

    def test_patch_preferences_updates_authenticated_users_preferences(self):
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            reverse('my_preferences'),
            {
                'email_notifications': False,
                'dashboard_alerts': False,
                'preferred_language': 'ar',
                'timezone': 'Asia/Jerusalem',
                'theme': UserPreferences.Theme.DARK,
                'metadata': {'compact_dashboard': True},
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        preferences = UserPreferences.objects.get(user=self.user)
        self.assertFalse(preferences.email_notifications)
        self.assertFalse(preferences.dashboard_alerts)
        self.assertEqual(preferences.preferred_language, 'ar')
        self.assertEqual(preferences.timezone, 'Asia/Jerusalem')
        self.assertEqual(preferences.theme, UserPreferences.Theme.DARK)
        self.assertEqual(preferences.metadata, {'compact_dashboard': True})

    def test_patch_preferences_cannot_change_owner(self):
        other = make_user()
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            reverse('my_preferences'),
            {'user': str(other.id), 'theme': UserPreferences.Theme.LIGHT},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        preferences = UserPreferences.objects.get(user=self.user)
        self.assertEqual(preferences.user, self.user)
        self.assertEqual(UserPreferences.objects.filter(user=other).count(), 0)


class VideoWorkflowAPITests(APITestCase):
    """Integration tests for upload, analysis, history, and dashboard endpoints."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.user = make_user(username='workflow', email='workflow@example.com')
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_upload_without_session_creates_default_domain_records(self):
        upload = SimpleUploadedFile('student-one.mp4', b'video bytes', content_type='video/mp4')

        response = self.client.post(reverse('videos_upload'), {'file': upload}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Exam.objects.filter(instructor=self.user).count(), 1)
        self.assertEqual(ExamSession.objects.filter(exam__instructor=self.user).count(), 1)
        self.assertEqual(Video.objects.count(), 1)
        self.assertEqual(response.data['session_status'], ExamSession.Status.PENDING)
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.ActionType.VIDEO_UPLOADED).count(), 1)

    def test_analyze_video_creates_job_alert_report_and_updates_history(self):
        session = make_session(exam=make_exam(instructor=self.user))
        upload = SimpleUploadedFile('exam.mp4', b'unique workflow bytes', content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        video = serializer.save()

        with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()):
            response = self.client.post(reverse('videos_analyze', kwargs={'pk': video.id}), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.COMPLETED)
        self.assertEqual(AnalysisJob.objects.filter(session=session, status=AnalysisJob.Status.COMPLETED).count(), 1)
        self.assertEqual(Alert.objects.filter(session=session).count(), 1)
        self.assertEqual(Report.objects.filter(session=session).count(), 1)

        history_response = self.client.get(reverse('videos_history'))
        self.assertEqual(history_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(history_response.data), 1)

    def test_dashboard_stats_include_owned_workflow_counts(self):
        session = make_session(exam=make_exam(instructor=self.user))
        session.status = ExamSession.Status.COMPLETED
        session.save(update_fields=['status'])
        Video.objects.create(
            session=session,
            file=SimpleUploadedFile('stored.mp4', b'abc', content_type='video/mp4'),
            original_filename='stored.mp4',
            content_type='video/mp4',
            size_bytes=3,
            file_hash=hashlib.sha256(b'abc').hexdigest(),
        )
        AnalysisJob.objects.create(session=session, status=AnalysisJob.Status.COMPLETED)
        Report.objects.create(
            session=session,
            overall_cheating_probability=0.75,
            total_alerts=1,
        )

        response = self.client.get(reverse('dashboard_stats'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['videos_total'], 1)
        self.assertEqual(response.data['videos_completed'], 1)
        self.assertEqual(response.data['analyses_total'], 1)
        self.assertEqual(response.data['cheating_reports_total'], 1)


class ThresholdsAndSystemAPITests(APITestCase):
    """Integration tests for settings-backed thresholds and admin system APIs."""

    def setUp(self):
        self.instructor = make_user(username='thresholds', email='thresholds@example.com')
        self.admin_user = make_user(
            username='systemadmin',
            email='systemadmin@example.com',
            role=User.Role.ADMIN,
        )

    def test_instructor_can_read_and_update_personal_thresholds(self):
        self.client.force_authenticate(self.instructor)

        read_response = self.client.get(reverse('thresholds'))
        self.assertEqual(read_response.status_code, status.HTTP_200_OK)
        self.assertIn('effective', read_response.data)

        update_response = self.client.patch(
            reverse('my_thresholds'),
            {'gaze_threshold': 0.55},
            format='json',
        )

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data['gaze_threshold'], 0.55)
        preferences = UserPreferences.objects.get(user=self.instructor)
        self.assertEqual(preferences.metadata['thresholds']['gaze_threshold'], 0.55)

    def test_global_threshold_updates_are_admin_only(self):
        self.client.force_authenticate(self.instructor)
        forbidden_response = self.client.patch(
            reverse('global_thresholds'),
            {'noise_threshold': 0.66},
            format='json',
        )
        self.assertEqual(forbidden_response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin_user)
        response = self.client.patch(
            reverse('global_thresholds'),
            {'noise_threshold': 0.66},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(float(SystemSettings.objects.get(setting_key='noise_threshold').setting_value), 0.66)

    def test_admin_can_read_system_metrics_and_logs(self):
        AuditLog.objects.create(
            user=self.admin_user,
            action=AuditLog.ActionType.LOGIN,
            target_resource='test',
        )
        self.client.force_authenticate(self.admin_user)

        metrics_response = self.client.get(reverse('system_metrics'))
        logs_response = self.client.get(reverse('system_logs'))

        self.assertEqual(metrics_response.status_code, status.HTTP_200_OK)
        self.assertIn('counts', metrics_response.data)
        self.assertEqual(logs_response.status_code, status.HTTP_200_OK)
        self.assertEqual(logs_response.data['count'], 1)


class DuplicateVideoHashTests(TestCase):
    """Issue 7 / FR4: duplicate video uploads must be rejected cleanly."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_duplicate_video_content_is_rejected(self):
        instructor = make_user()
        exam = make_exam(instructor=instructor)
        session1 = make_session(exam=exam, student_identifier='s1')
        session2 = make_session(exam=exam, student_identifier='s2')
        content = b'identical video bytes'

        upload1 = SimpleUploadedFile('exam1.mp4', content, content_type='video/mp4')
        ser1 = VideoUploadSerializer(
            data={'session': str(session1.id), 'file': upload1},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(ser1.is_valid(), ser1.errors)
        ser1.save()

        upload2 = SimpleUploadedFile('exam2.mp4', content, content_type='video/mp4')
        ser2 = VideoUploadSerializer(
            data={'session': str(session2.id), 'file': upload2},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(ser2.is_valid(), ser2.errors)

        with self.assertRaises(Exception) as ctx:
            ser2.save()

        self.assertIn('already been uploaded', str(ctx.exception))


class ExamSessionDefaultStatusTests(TestCase):
    """Issue 13: ExamSession should default to PENDING."""

    def test_default_status_is_pending(self):
        session = make_session()

        self.assertEqual(session.status, ExamSession.Status.PENDING)

    def test_status_can_be_set_to_processing(self):
        session = make_session()
        session.status = ExamSession.Status.PROCESSING
        session.save()
        session.refresh_from_db()

        self.assertEqual(session.status, ExamSession.Status.PROCESSING)


class AlertCreateSerializerTests(TestCase):
    """Issue 14: ownership validation on AlertCreateSerializer."""

    def test_instructor_cannot_create_alert_for_another_instructors_session(self):
        owner = make_user(email='owner@example.com', username='owner')
        other = make_user(email='other@example.com', username='other')
        session = make_session(exam=make_exam(instructor=owner))

        serializer = AlertCreateSerializer(
            data={
                'session': str(session.id),
                'timestamp_sec': 10,
                'behavior_type': Alert.BehaviorType.PHONE_DETECTED,
                'confidence_score': 0.9,
            },
            context={'request': request_for(other)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('session', serializer.errors)

    def test_owner_can_create_alert_for_own_session(self):
        owner = make_user()
        session = make_session(exam=make_exam(instructor=owner))

        serializer = AlertCreateSerializer(
            data={
                'session': str(session.id),
                'timestamp_sec': 10,
                'behavior_type': Alert.BehaviorType.PHONE_DETECTED,
                'confidence_score': 0.9,
            },
            context={'request': request_for(owner)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class UserRoleTests(TestCase):
    """Issue 15: User.role field enforcement."""

    def test_role_must_be_valid_choice(self):
        user = User(
            username='baduser',
            email='bad@example.com',
            role='STUDENT',
        )

        with self.assertRaises(ValidationError):
            user.full_clean()

    def test_role_is_required(self):
        user = User(
            username='norole',
            email='norole@example.com',
            role='',
        )

        with self.assertRaises(ValidationError):
            user.full_clean()


class ReportModelTests(TestCase):
    """Issue 2 / FR11: Report model validation."""

    def test_cheating_probability_must_be_between_zero_and_one(self):
        session = make_session()
        report = Report(
            session=session,
            overall_cheating_probability=1.5,
        )

        with self.assertRaises(ValidationError):
            report.full_clean()

    def test_valid_report_can_be_created(self):
        session = make_session()
        report = Report.objects.create(
            session=session,
            overall_cheating_probability=0.65,
            total_alerts=3,
            alerts_by_type={'PHONE_DETECTED': 2, 'LOOKING_AWAY': 1},
            summary='Moderate risk detected.',
        )

        self.assertEqual(report.overall_cheating_probability, 0.65)
        self.assertEqual(report.total_alerts, 3)


class ReportCreateSerializerTests(TestCase):
    """Report serializer ownership validation."""

    def test_instructor_cannot_create_report_for_another_instructors_session(self):
        owner = make_user(email='owner@example.com', username='owner')
        other = make_user(email='other@example.com', username='other')
        session = make_session(exam=make_exam(instructor=owner))

        serializer = ReportCreateSerializer(
            data={
                'session': str(session.id),
                'overall_cheating_probability': 0.5,
                'total_alerts': 1,
            },
            context={'request': request_for(other)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('session', serializer.errors)


class AsyncQueueWiringTests(APITestCase):
    """Task 2: analysis is dispatched through the django-rq queue.

    Two paths are exercised without any external Redis:
    * **eager** (``RQ_ASYNC`` off, the default) — the worker runs in-process so
      the endpoint returns the finished report with ``200 OK``;
    * **async** (queue reports ``is_async``) — the job is handed to the queue
      and the endpoint answers ``202 Accepted`` while it is still ``QUEUED``.
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.user = make_user(username='queue', email='queue@example.com')
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _make_video(self, content=b'queue-wiring bytes'):
        session = make_session(exam=make_exam(instructor=self.user))
        upload = SimpleUploadedFile('queue.mp4', content, content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def test_eager_analyze_completes_job_and_returns_report(self):
        video = self._make_video()

        with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()):
            response = self.client.post(reverse('videos_analyze', kwargs={'pk': video.id}), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], AnalysisJob.Status.COMPLETED)
        self.assertIn('job_id', response.data)
        self.assertIn('analysis', response.data)
        self.assertEqual(len(response.data['alerts']), 1)

        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.status, AnalysisJob.Status.COMPLETED)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.completed_at)
        # The worker records the completion audit trail with no HTTP request.
        for action in (AuditLog.ActionType.ANALYSIS_STARTED,
                       AuditLog.ActionType.ANALYSIS_COMPLETED,
                       AuditLog.ActionType.REPORT_GENERATED):
            self.assertTrue(
                AuditLog.objects.filter(action=action).exists(),
                f'missing audit entry for {action}',
            )

    def test_async_analyze_enqueues_job_and_returns_202(self):
        video = self._make_video()
        fake_queue = mock.Mock()
        fake_queue.is_async = True

        with mock.patch('django_rq.get_queue', return_value=fake_queue):
            response = self.client.post(reverse('videos_analyze', kwargs={'pk': video.id}), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['status'], AnalysisJob.Status.QUEUED)
        self.assertIn('job_id', response.data)
        self.assertNotIn('analysis', response.data)
        fake_queue.enqueue.assert_called_once()
        self.assertIs(fake_queue.enqueue.call_args.args[0], run_analysis)

        # The job is persisted as QUEUED and no report exists until a worker runs.
        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.status, AnalysisJob.Status.QUEUED)
        self.assertFalse(Report.objects.filter(session=video.session).exists())
        self.assertEqual(
            ExamSession.objects.get(id=video.session.id).status,
            ExamSession.Status.PROCESSING,
        )

    def test_worker_marks_job_failed_on_error(self):
        video = self._make_video()
        job = AnalysisJob.objects.create(session=video.session, status=AnalysisJob.Status.QUEUED)

        with mock.patch('apis.tasks.build_ai_report', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                run_analysis(str(job.id), actor_id=str(self.user.id))

        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.FAILED)
        self.assertIn('boom', job.error_message)
        self.assertEqual(
            ExamSession.objects.get(id=video.session.id).status,
            ExamSession.Status.FAILED,
        )


class PreanalyzeDemosCommandTests(TestCase):
    """Task 1.5: the ``preanalyze_demos`` management command.

    Exercises both the import-only path (``--no-analyze``) and the full
    analyze path with the AI pipeline mocked, so no model weights or real
    video decoding are required.
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.clips_dir = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)
        shutil.rmtree(self.clips_dir, ignore_errors=True)

    def _write_clip(self, name='demo-one.mp4', content=b'demo clip bytes'):
        path = Path(self.clips_dir) / name
        path.write_bytes(content)
        return str(path)

    def test_no_analyze_imports_video_and_queues_job(self):
        clip = self._write_clip()

        call_command(
            'preanalyze_demos', clip, '--no-analyze',
            '--instructor', 'demo@example.com', '--exam', 'Demo Exam',
            verbosity=0,
        )

        exam = Exam.objects.get(name='Demo Exam')
        self.assertEqual(exam.instructor.email, 'demo@example.com')
        self.assertEqual(exam.instructor.role, User.Role.INSTRUCTOR)
        session = ExamSession.objects.get(exam=exam)
        self.assertEqual(session.student_identifier, 'demo-one')
        self.assertTrue(hasattr(session, 'video'))
        job = AnalysisJob.objects.get(session=session)
        self.assertEqual(job.status, AnalysisJob.Status.QUEUED)
        # No analysis ran, so no report yet.
        self.assertFalse(Report.objects.filter(session=session).exists())

    def test_analyze_path_seeds_alerts_and_report(self):
        clip = self._write_clip(name='cheater.mp4')

        with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()):
            call_command(
                'preanalyze_demos', '--dir', self.clips_dir,
                '--instructor', 'demo@example.com', verbosity=0,
            )

        session = ExamSession.objects.get(student_identifier='cheater')
        job = AnalysisJob.objects.get(session=session)
        self.assertEqual(job.status, AnalysisJob.Status.COMPLETED)
        self.assertEqual(session.status, ExamSession.Status.COMPLETED)
        self.assertEqual(Alert.objects.filter(session=session).count(), 1)
        report = Report.objects.get(session=session)
        self.assertEqual(report.total_alerts, 1)
        # Full audit trail recorded by the command + worker.
        for action in (AuditLog.ActionType.ANALYSIS_STARTED,
                       AuditLog.ActionType.ANALYSIS_COMPLETED,
                       AuditLog.ActionType.REPORT_GENERATED):
            self.assertTrue(
                AuditLog.objects.filter(action=action).exists(),
                f'missing audit entry for {action}',
            )

    def test_duplicate_clip_is_skipped_without_force(self):
        clip = self._write_clip()
        with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()):
            call_command('preanalyze_demos', clip, '--instructor', 'demo@example.com', verbosity=0)
            # Second run with identical content imports nothing new.
            call_command('preanalyze_demos', clip, '--instructor', 'demo@example.com', verbosity=0)

        self.assertEqual(Video.objects.count(), 1)
        self.assertEqual(ExamSession.objects.count(), 1)

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
    Alert, AnalysisJob, AuditLog, Exam, ExamSession, Notification, Report,
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


def fake_video_bytes(unique=b''):
    """Bytes that pass VideoUploadSerializer's container sniff (N2).

    Begins with a minimal ISO-BMFF ``ftyp`` header so the content-sniff in
    :meth:`VideoUploadSerializer._looks_like_video` accepts the fixture as a
    real video. Append ``unique`` bytes to vary the SHA-256 hash between
    uploads (dedup tests).
    """
    return b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00' + unique


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
            behavior_type=Alert.BehaviorType.LOOKING_AWAY,
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
        content = fake_video_bytes(b'metadata content')
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

    def test_rejects_non_video_disguised_as_mp4(self):
        """N2: a non-video renamed to .mp4 with no Content-Type must not pass.

        The old check trusted the (absent) Content-Type and the extension, so
        arbitrary bytes in a ``*.mp4`` would reach OpenCV/ffmpeg.
        """
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        upload = SimpleUploadedFile(
            'malware.mp4', b'MZ\x90\x00 not a real video at all', content_type=None,
        )
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(instructor)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('file', serializer.errors)

    def test_rejects_empty_file(self):
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        upload = SimpleUploadedFile('empty.mp4', b'', content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(instructor)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('empty', str(serializer.errors['file']).lower())

    def test_rejects_video_exceeding_max_upload_size(self):
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        upload = SimpleUploadedFile(
            'big.mp4', fake_video_bytes(b'x' * 200), content_type='video/mp4',
        )
        with override_settings(MAX_UPLOAD_SIZE_BYTES=16):
            serializer = VideoUploadSerializer(
                data={'session': str(session.id), 'file': upload},
                context={'request': request_for(instructor)},
            )
            self.assertFalse(serializer.is_valid())
        self.assertIn('maximum upload size', str(serializer.errors['file']).lower())

    def test_accepts_webm_and_avi_container_signatures(self):
        instructor = make_user()
        exam = make_exam(instructor=instructor)

        webm = SimpleUploadedFile(
            'clip.webm', b'\x1a\x45\xdf\xa3' + b'\x00' * 12, content_type='video/webm',
        )
        ser_webm = VideoUploadSerializer(
            data={'session': str(make_session(exam=exam, student_identifier='w').id), 'file': webm},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(ser_webm.is_valid(), ser_webm.errors)

        avi = SimpleUploadedFile(
            'clip.avi', b'RIFF\x00\x00\x00\x00AVI \x00\x00\x00\x00', content_type='video/x-msvideo',
        )
        ser_avi = VideoUploadSerializer(
            data={'session': str(make_session(exam=exam, student_identifier='a').id), 'file': avi},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(ser_avi.is_valid(), ser_avi.errors)

    def test_instructor_cannot_upload_video_for_another_instructors_session(self):
        owner = make_user(email='owner@example.com', username='owner')
        other = make_user(email='other@example.com', username='other')
        session = make_session(exam=make_exam(instructor=owner))
        upload = SimpleUploadedFile('exam.mp4', fake_video_bytes(b'owner'), content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(other)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('session', serializer.errors)

    def test_negative_duration_is_rejected(self):
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        upload = SimpleUploadedFile('exam.mp4', fake_video_bytes(b'negdur'), content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload, 'duration_seconds': -1},
            context={'request': request_for(instructor)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('duration_seconds', serializer.errors)


class VideoReadSerializerAnnotatedVideoTests(TestCase):
    """H8: VideoReadSerializer exposes the annotated analysis video URL."""

    def _video(self, session):
        return Video.objects.create(
            session=session,
            file='exam-videos/test/test.mp4',
            original_filename='test.mp4',
            file_hash='a' * 64,
        )

    def test_annotated_video_url_from_job_metadata(self):
        from apis.serializers import VideoReadSerializer

        session = make_session()
        video = self._video(session)
        AnalysisJob.objects.create(
            session=session,
            metadata={'annotated_video_url': '/media/exam-videos/test/test.annotated.mp4'},
        )

        # No request in context → the raw stored URL is returned verbatim.
        data = VideoReadSerializer(video, context={}).data
        self.assertEqual(
            data['annotated_video_url'],
            '/media/exam-videos/test/test.annotated.mp4',
        )

    def test_annotated_video_url_none_without_metadata_key(self):
        from apis.serializers import VideoReadSerializer

        session = make_session()
        video = self._video(session)
        AnalysisJob.objects.create(session=session, metadata={})

        data = VideoReadSerializer(video, context={}).data
        self.assertIsNone(data['annotated_video_url'])

    def test_annotated_video_url_none_without_job(self):
        from apis.serializers import VideoReadSerializer

        session = make_session()
        video = self._video(session)

        data = VideoReadSerializer(video, context={}).data
        self.assertIsNone(data['annotated_video_url'])


class AlertSerializerTests(TestCase):
    """Verify the review and un-review workflow via AlertReviewSerializer."""

    def test_alert_review_serializer_reviews_alert(self):
        reviewer = make_user()
        alert = Alert.objects.create(
            session=make_session(),
            timestamp_sec=20,
            behavior_type=Alert.BehaviorType.PHONE_DETECTED,
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
            behavior_type=Alert.BehaviorType.PHONE_DETECTED,
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
        upload = SimpleUploadedFile('student-one.mp4', fake_video_bytes(b'student-one'), content_type='video/mp4')

        response = self.client.post(reverse('videos_upload'), {'file': upload}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Exam.objects.filter(instructor=self.user).count(), 1)
        self.assertEqual(ExamSession.objects.filter(exam__instructor=self.user).count(), 1)
        self.assertEqual(Video.objects.count(), 1)
        self.assertEqual(response.data['session_status'], ExamSession.Status.PENDING)
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.ActionType.VIDEO_UPLOADED).count(), 1)
        # 201 advertises the new resource via Location (no body-parse needed).
        self.assertIn(str(Video.objects.get().id), response['Location'])

    def test_analyze_video_creates_job_alert_report_and_updates_history(self):
        session = make_session(exam=make_exam(instructor=self.user))
        upload = SimpleUploadedFile('exam.mp4', fake_video_bytes(b'workflow'), content_type='video/mp4')
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

    def test_user_threshold_sensitivity_reaches_pipeline(self):
        """The saved AIThresholds sensitivity is fed to analyze_video (C1 bridge).

        Both detector confidence floors must reflect the exam owner's effective
        gaze_threshold rather than the static pipeline defaults.
        """
        from apis.services import update_user_thresholds

        update_user_thresholds(self.user, {'gaze_threshold': 0.3})

        session = make_session(exam=make_exam(instructor=self.user))
        upload = SimpleUploadedFile('exam-threshold.mp4', fake_video_bytes(b'threshold'), content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        video = serializer.save()

        with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()) as mocked:
            response = self.client.post(reverse('videos_analyze', kwargs={'pk': video.id}), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mocked.call_count, 1)
        _args, kwargs = mocked.call_args
        self.assertAlmostEqual(kwargs['object_confidence'], 0.3)
        self.assertAlmostEqual(kwargs['pose_confidence'], 0.3)

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


class ValidateThresholdsTests(APITestCase):
    """M3: validate_thresholds rejects unknown keys instead of dropping them."""

    def test_unknown_key_is_rejected(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from apis.services import validate_thresholds

        with self.assertRaises(DRFValidationError) as ctx:
            validate_thresholds({'gaze_treshold': 0.5})  # typo'd key
        self.assertIn('gaze_treshold', ctx.exception.detail)

    def test_unknown_key_rejected_even_alongside_valid_keys(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from apis.services import validate_thresholds

        with self.assertRaises(DRFValidationError):
            validate_thresholds({'gaze_threshold': 0.5, 'bogus': 0.5})

    def test_known_keys_pass_through_as_floats(self):
        from apis.services import validate_thresholds

        cleaned = validate_thresholds({'gaze_threshold': '0.5', 'noise_threshold': 0.7})
        self.assertEqual(cleaned, {'gaze_threshold': 0.5, 'noise_threshold': 0.7})

    def test_out_of_range_and_non_numeric_still_rejected(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from apis.services import validate_thresholds

        with self.assertRaises(DRFValidationError):
            validate_thresholds({'gaze_threshold': 2.0})
        with self.assertRaises(DRFValidationError):
            validate_thresholds({'gaze_threshold': 'abc'})

    def test_unknown_key_returns_400_from_api(self):
        instructor = make_user(username='m3user', email='m3@example.com')
        self.client.force_authenticate(instructor)
        response = self.client.patch(
            reverse('my_thresholds'), {'gaze_treshold': 0.5}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ThresholdPayloadCoercionTests(TestCase):
    """M2: threshold_payload tolerates non-numeric stored values."""

    def test_non_numeric_stored_value_falls_back_to_default(self):
        from apis.services import THRESHOLD_DEFAULTS, threshold_payload

        payload = threshold_payload({'gaze_threshold': 'corrupt'})
        self.assertEqual(payload['gaze_threshold'],
                         float(THRESHOLD_DEFAULTS['gaze_threshold']))

    def test_none_stored_value_falls_back_to_default(self):
        from apis.services import THRESHOLD_DEFAULTS, threshold_payload

        payload = threshold_payload({'noise_threshold': None})
        self.assertEqual(payload['noise_threshold'],
                         float(THRESHOLD_DEFAULTS['noise_threshold']))

    def test_numeric_string_is_preserved(self):
        from apis.services import threshold_payload

        payload = threshold_payload({'gaze_threshold': '0.42'})
        self.assertEqual(payload['gaze_threshold'], 0.42)

    def test_get_global_thresholds_survives_corrupt_setting(self):
        from apis.services import get_global_thresholds

        SystemSettings.objects.update_or_create(
            setting_key='gaze_threshold',
            defaults={'setting_value': 'not-a-number'},
        )
        # Must not raise; the corrupt value degrades to the default.
        payload = get_global_thresholds()
        self.assertIsInstance(payload['gaze_threshold'], float)


class SendWorkspaceInviteMembershipTests(TestCase):
    """M13: send_workspace_invite refuses to re-invite an existing member."""

    def _setup(self):
        from apis.models import Workspace

        dean = make_user(username='dean13', email='dean13@example.com',
                         role=User.Role.DEAN)
        instructor = make_user(username='inst13', email='inst13@example.com')
        workspace = Workspace.objects.create(name='Physics Dept', owner=dean)
        return dean, instructor, workspace

    def test_invite_rejected_for_existing_member(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from apis.models import WorkspaceInvite, WorkspaceMembership
        from apis.services import send_workspace_invite

        dean, instructor, workspace = self._setup()
        WorkspaceMembership.objects.create(workspace=workspace, instructor=instructor)

        with self.assertRaises(DRFValidationError):
            send_workspace_invite(dean, instructor, workspace=workspace)
        # No duplicate invite row created for the existing member.
        self.assertFalse(WorkspaceInvite.objects.filter(
            workspace=workspace, instructor=instructor).exists())

    def test_invite_allowed_for_non_member(self):
        from apis.models import WorkspaceInvite
        from apis.services import send_workspace_invite

        dean, instructor, workspace = self._setup()
        invite = send_workspace_invite(dean, instructor, workspace=workspace)

        self.assertEqual(invite.workspace_id, workspace.id)
        self.assertTrue(WorkspaceInvite.objects.filter(id=invite.id).exists())

    def test_exam_only_invite_not_blocked_by_membership(self):
        from apis.services import send_workspace_invite

        dean, instructor, _workspace = self._setup()
        exam = make_exam(instructor=dean)
        # No workspace target → membership guard does not apply.
        invite = send_workspace_invite(dean, instructor, exam=exam)
        self.assertEqual(invite.exam_id, exam.id)


class InviteRespondMethodTests(APITestCase):
    """Invite accept/decline must be POST, not a GET with side effects."""

    def _invite(self):
        from apis.models import Workspace, WorkspaceInvite

        dean = make_user(username='deanIR', email='deanir@example.com',
                         role=User.Role.DEAN)
        instructor = make_user(username='instIR', email='instir@example.com')
        workspace = Workspace.objects.create(name='Stats Dept', owner=dean)
        invite = WorkspaceInvite.objects.create(
            dean=dean, instructor=instructor, workspace=workspace)
        return invite

    def test_get_is_rejected_with_no_side_effect(self):
        from apis.models import WorkspaceInvite

        invite = self._invite()
        response = self.client.get(
            reverse('invite_accept', kwargs={'token': invite.token}))

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        invite.refresh_from_db()
        # The bot's GET prefetch must not have accepted the invite.
        self.assertEqual(invite.status, WorkspaceInvite.Status.PENDING)

    def test_post_accept_joins_workspace(self):
        from apis.models import WorkspaceInvite, WorkspaceMembership

        invite = self._invite()
        response = self.client.post(
            reverse('invite_accept', kwargs={'token': invite.token}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invite.refresh_from_db()
        self.assertEqual(invite.status, WorkspaceInvite.Status.ACCEPTED)
        self.assertTrue(WorkspaceMembership.objects.filter(
            workspace=invite.workspace, instructor=invite.instructor).exists())

    def test_post_decline_sets_declined(self):
        from apis.models import WorkspaceInvite

        invite = self._invite()
        response = self.client.post(
            reverse('invite_decline', kwargs={'token': invite.token}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invite.refresh_from_db()
        self.assertEqual(invite.status, WorkspaceInvite.Status.DECLINED)


class UploadSessionBoundTests(TestCase):
    """N4: get_available_upload_session must not probe identifiers forever.

    Each probe is a `get_or_create` round trip, so an unbounded loop lets one
    identifier with many occupied sessions fire thousands of queries. The loop
    is capped at MAX_UPLOAD_SESSION_ATTEMPTS, after which it raises rather than
    spinning.
    """

    def _upload(self):
        return SimpleNamespace(name='john.mp4')

    def _occupy(self, instructor, upload, ident, n):
        """Allocate *n* sessions for *ident* and attach a video to each."""
        from apis.services import get_available_upload_session

        sessions = []
        for i in range(n):
            session = get_available_upload_session(
                instructor, upload, student_identifier=ident)
            Video.objects.create(
                session=session,
                original_filename='x.mp4',
                file='x.mp4',
                file_hash=f'hash-{i}-{uuid.uuid4().hex}',
            )
            sessions.append(session)
        return sessions

    def test_reuses_next_free_identifier_variant(self):
        from apis.services import get_available_upload_session

        instructor = make_user()
        upload = self._upload()
        first, = self._occupy(instructor, upload, 'john', 1)

        # 'john' is taken, so the next allocation must fall through to 'john-2'.
        nxt = get_available_upload_session(
            instructor, upload, student_identifier='john')

        self.assertNotEqual(nxt.id, first.id)
        self.assertEqual(nxt.student_identifier, 'john-2')
        self.assertFalse(hasattr(nxt, 'video'))

    def test_raises_after_max_attempts_exhausted(self):
        from apis import services
        from rest_framework.exceptions import ValidationError as DRFValidationError

        instructor = make_user()
        upload = self._upload()

        # Cap the loop low so the test stays fast; occupy every slot it will probe.
        with mock.patch.object(services, 'MAX_UPLOAD_SESSION_ATTEMPTS', 3):
            self._occupy(instructor, upload, 'john', 3)

            with self.assertRaises(DRFValidationError) as ctx:
                services.get_available_upload_session(
                    instructor, upload, student_identifier='john')

        self.assertIn('john', str(ctx.exception.detail))
        # No runaway session creation past the cap: john, john-2, john-3 only.
        self.assertEqual(
            ExamSession.objects.filter(
                student_identifier__startswith='john').count(),
            3,
        )


class UploadRewindHardeningTests(TestCase):
    """N5: the post-hash rewind must be verified so a stuck descriptor can't
    silently store a truncated video."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_full_content_is_stored_after_rewind(self):
        """The happy path: hashing consumes the stream, yet the saved file holds
        every byte — proof the rewind put the descriptor back to the start."""
        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        content = fake_video_bytes(b'the complete payload must survive hashing')
        upload = SimpleUploadedFile('exam.mp4', content, content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        video = serializer.save()

        video.file.open('rb')
        try:
            stored = video.file.read()
        finally:
            video.file.close()
        self.assertEqual(stored, content)

    def test_unverified_rewind_is_rejected_without_writing_a_row(self):
        """A descriptor left at EOF (a seek that didn't take) must raise rather
        than persist a truncated file."""
        from rest_framework.exceptions import ValidationError as DRFValidationError

        instructor = make_user()
        session = make_session(exam=make_exam(instructor=instructor))
        content = fake_video_bytes(b'stuck stream')

        class _StuckFile:
            """File-like whose seek(0) silently fails to rewind."""
            name = 'exam.mp4'
            content_type = 'video/mp4'
            size = len(content)

            def chunks(self):
                yield content

            def seek(self, _pos):
                pass  # no-op: the rewind does not take effect

            def tell(self):
                return len(content)  # still at EOF

        serializer = VideoUploadSerializer(
            context={'request': request_for(instructor)})
        with self.assertRaises(DRFValidationError):
            serializer.create({'file': _StuckFile(), 'session': session})

        self.assertFalse(Video.objects.filter(session=session).exists())


class DuplicateVideoHashTests(TestCase):
    """Issue 7 / FR4: duplicate video uploads must be rejected cleanly."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_same_file_allowed_for_different_sessions_but_not_same_session(self):
        instructor = make_user()
        exam = make_exam(instructor=instructor)
        session1 = make_session(exam=exam, student_identifier='s1')
        session2 = make_session(exam=exam, student_identifier='s2')
        content = fake_video_bytes(b'identical')

        upload1 = SimpleUploadedFile('exam1.mp4', content, content_type='video/mp4')
        ser1 = VideoUploadSerializer(
            data={'session': str(session1.id), 'file': upload1},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(ser1.is_valid(), ser1.errors)
        ser1.save()

        # Same file, DIFFERENT session → now allowed (per-session uniqueness).
        upload2 = SimpleUploadedFile('exam2.mp4', content, content_type='video/mp4')
        ser2 = VideoUploadSerializer(
            data={'session': str(session2.id), 'file': upload2},
            context={'request': request_for(instructor)},
        )
        self.assertTrue(ser2.is_valid(), ser2.errors)
        ser2.save()  # no exception — different session

        # Same session that already has a video → rejected at validation time
        # (the OneToOne session guard fires before any save).
        upload3 = SimpleUploadedFile('exam1-again.mp4', content, content_type='video/mp4')
        ser3 = VideoUploadSerializer(
            data={'session': str(session1.id), 'file': upload3},
            context={'request': request_for(instructor)},
        )
        self.assertFalse(ser3.is_valid())
        self.assertIn('already exists', str(ser3.errors))


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


class ReportProbabilityTests(TestCase):
    """C6 + N1: the overall cheating-probability formula.

    Uses lightweight stand-ins (only ``behavior_type`` + ``confidence_score`` are
    read) so the maths is tested in isolation from the ORM.
    """

    @staticmethod
    def _alert(behavior_type, confidence):
        return SimpleNamespace(behavior_type=behavior_type, confidence_score=confidence)

    def test_no_alerts_is_zero(self):
        from apis.services import _report_probability

        self.assertEqual(_report_probability([]), 0.0)

    def test_monotonic_more_evidence_never_lowers_score(self):
        """C6: adding alerts must not decrease the probability."""
        from apis.services import _report_probability

        one = [self._alert(Alert.BehaviorType.LOOKING_AWAY, 0.5)]
        many = one + [
            self._alert(Alert.BehaviorType.LOOKING_AWAY, 0.5),
            self._alert(Alert.BehaviorType.LOOKING_AWAY, 0.5),
        ]
        self.assertGreaterEqual(_report_probability(many), _report_probability(one))

    def test_phone_alone_does_not_drop_when_glances_added(self):
        """C6 regression: 1 phone + several glances >= 1 phone alone."""
        from apis.services import _report_probability

        phone = [self._alert(Alert.BehaviorType.PHONE_DETECTED, 1.0)]
        phone_plus = phone + [
            self._alert(Alert.BehaviorType.LOOKING_AWAY, 0.5) for _ in range(5)
        ]
        self.assertGreaterEqual(_report_probability(phone_plus), _report_probability(phone))

    def test_behavior_type_aware_phone_outweighs_looking_away(self):
        """N1: a saturated head-turn must score well below a phone."""
        from apis.services import _report_probability

        looking = [self._alert(Alert.BehaviorType.LOOKING_AWAY, 1.0)]
        phone = [self._alert(Alert.BehaviorType.PHONE_DETECTED, 1.0)]
        self.assertLess(_report_probability(looking), _report_probability(phone))
        # A single sustained head-turn alone must never read as near-certain cheating.
        self.assertLessEqual(_report_probability(looking), 0.5)

    def test_capped_below_one(self):
        from apis.services import _report_probability

        certain = [self._alert(Alert.BehaviorType.PHONE_DETECTED, 1.0)]
        self.assertLessEqual(_report_probability(certain), 0.99)

    def test_scoring_uses_raw_confidence_not_severity_bucket(self):
        """H1: two alerts in the *same* severity bucket but with different raw
        confidences must score differently.

        ``_severity_for`` collapses confidence into 3 buckets (LOW/MEDIUM/HIGH).
        The old formula keyed scoring on that bucket, so 0.51 and 0.79 (both
        MEDIUM) were indistinguishable. Scoring now reads ``confidence_score``
        directly, so the raw precision must survive into the report.
        """
        from apis.services import _report_probability, _severity_for

        low_mid = self._alert(Alert.BehaviorType.LOOKING_AWAY, 0.51)
        high_mid = self._alert(Alert.BehaviorType.LOOKING_AWAY, 0.79)
        # Both land in the same severity bucket...
        self.assertEqual(_severity_for(0.51), _severity_for(0.79))
        self.assertEqual(_severity_for(0.51), Alert.Severity.MEDIUM)
        # ...yet the report distinguishes them by raw confidence.
        self.assertNotEqual(
            _report_probability([low_mid]), _report_probability([high_mid]),
        )
        self.assertLess(_report_probability([low_mid]), _report_probability([high_mid]))


class ClearSessionEvidenceTests(TestCase):
    """C5: re-analysis wipes a session's prior clip/snapshot files on disk."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _seed_evidence(self, session_id):
        root = Path(self.media_root)
        files = []
        for subdir in ('clips', 'snapshots'):
            target = root / subdir / str(session_id)
            target.mkdir(parents=True, exist_ok=True)
            artifact = target / 'old-alert.bin'
            artifact.write_bytes(b'stale')
            files.append(artifact)
        return files

    def test_removes_prior_session_evidence(self):
        from apis.services import clear_session_evidence

        session = make_session()
        files = self._seed_evidence(session.id)
        self.assertTrue(all(f.exists() for f in files))

        clear_session_evidence(session)

        for subdir in ('clips', 'snapshots'):
            self.assertFalse((Path(self.media_root) / subdir / str(session.id)).exists())

    def test_leaves_other_sessions_untouched(self):
        from apis.services import clear_session_evidence

        target = make_session()
        other = make_session()
        self._seed_evidence(target.id)
        other_files = self._seed_evidence(other.id)

        clear_session_evidence(target)

        self.assertTrue(all(f.exists() for f in other_files))

    def test_missing_directories_is_a_noop(self):
        from apis.services import clear_session_evidence

        session = make_session()
        # No evidence ever written — must not raise.
        clear_session_evidence(session)


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

    def _make_video(self, content=None):
        content = content if content is not None else fake_video_bytes(b'queue-wiring')
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
        # 202 carries client-guidance headers: when to poll, and where.
        self.assertEqual(response['Retry-After'], '5')
        self.assertIn(str(video.id), response['Location'])

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

    def test_truncate_error_caps_long_tracebacks(self):
        """M10: the helper bounds the stored text and marks the clip."""
        from apis.tasks import _MAX_ERROR_MESSAGE_CHARS, _truncate_error

        short = 'ValueError: boom'
        self.assertEqual(_truncate_error(short), short)  # short text untouched
        self.assertEqual(_truncate_error(''), '')
        self.assertEqual(_truncate_error(None), '')

        huge = 'x' * (_MAX_ERROR_MESSAGE_CHARS + 5000)
        capped = _truncate_error(huge)
        self.assertTrue(capped.startswith('x' * _MAX_ERROR_MESSAGE_CHARS))
        self.assertTrue(capped.endswith('[truncated]'))
        self.assertLess(len(capped), len(huge))

    def test_worker_stores_truncated_error_message(self):
        """M10: a multi-KB traceback is not persisted verbatim onto the job."""
        from apis.tasks import _MAX_ERROR_MESSAGE_CHARS

        video = self._make_video()
        job = AnalysisJob.objects.create(session=video.session, status=AnalysisJob.Status.QUEUED)

        giant = 'CUDA out of memory\n' + ('frame ' * 4000)
        with mock.patch('apis.tasks.build_ai_report', side_effect=RuntimeError(giant)):
            with self.assertRaises(RuntimeError):
                run_analysis(str(job.id), actor_id=str(self.user.id))

        job.refresh_from_db()
        # Head preserved (the actual cause), total length bounded.
        self.assertTrue(job.error_message.startswith('CUDA out of memory'))
        self.assertLessEqual(
            len(job.error_message), _MAX_ERROR_MESSAGE_CHARS + len('\n…[truncated]'),
        )

    def test_failure_is_audited_and_notified(self):
        """H5/H12: a failed analysis writes an audit entry and notifies the owner."""
        from apis.models import Notification

        video = self._make_video()
        job = AnalysisJob.objects.create(session=video.session, status=AnalysisJob.Status.QUEUED)

        with mock.patch('apis.tasks.build_ai_report', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                run_analysis(str(job.id), actor_id=str(self.user.id))

        # An ANALYSIS_FAILED audit entry exists, attributed to the triggering user.
        audit = AuditLog.objects.filter(action=AuditLog.ActionType.ANALYSIS_FAILED)
        self.assertTrue(audit.exists())
        self.assertEqual(audit.first().user_id, self.user.id)

        # The exam's instructor (self.user) gets an in-app failure notification.
        notif = Notification.objects.filter(
            recipient=self.user,
            notif_type=Notification.NotifType.ANALYSIS_FAILED,
        )
        self.assertTrue(notif.exists())
        self.assertEqual(notif.first().metadata.get('session_id'), str(video.session.id))

    def test_notification_failure_does_not_mask_original_error(self):
        """H5/H12: a broken notification path must not swallow the analysis error."""
        video = self._make_video()
        job = AnalysisJob.objects.create(session=video.session, status=AnalysisJob.Status.QUEUED)

        with mock.patch('apis.tasks.build_ai_report', side_effect=RuntimeError('boom')):
            with mock.patch('apis.tasks.create_notification', side_effect=ValueError('notify down')):
                # The ORIGINAL RuntimeError must still surface, not the ValueError.
                with self.assertRaises(RuntimeError):
                    run_analysis(str(job.id), actor_id=str(self.user.id))

        # And the FAILED state is still persisted despite the notification error.
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.FAILED)

    def test_run_analysis_skips_already_completed_job(self):
        """C7: re-invoking run_analysis on a COMPLETED job is a no-op."""
        video = self._make_video()
        job = AnalysisJob.objects.create(session=video.session, status=AnalysisJob.Status.QUEUED)

        with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()):
            report_id = run_analysis(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.COMPLETED)

        # A duplicate enqueue / second worker must NOT re-run the pipeline; the
        # existing report id is returned unchanged.
        with mock.patch('apis.tasks.build_ai_report') as mocked_build:
            skipped = run_analysis(str(job.id))

        mocked_build.assert_not_called()
        self.assertEqual(skipped, report_id)

    def test_run_analysis_skips_job_already_processing(self):
        """C7: a job another worker is already PROCESSING is not picked up again."""
        video = self._make_video()
        job = AnalysisJob.objects.create(session=video.session, status=AnalysisJob.Status.PROCESSING)

        with mock.patch('apis.tasks.build_ai_report') as mocked_build:
            result = run_analysis(str(job.id))

        mocked_build.assert_not_called()
        self.assertIsNone(result)
        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJob.Status.PROCESSING)


class DeadJobFieldsWiringTests(APITestCase):
    """H2/H3/M9: the previously-dead AnalysisJob fields are now real.

    * ``frame_sample_rate`` is honoured by the pipeline (H2) and defaults to the
      pipeline's truthful rate instead of a misleading 1 (M9).
    * ``ai_model_version`` is provenance the pipeline stamps with the model that
      actually ran, not an ignored client input (H3).
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.user = make_user(username='wire', email='wire@example.com')
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _make_video(self):
        session = make_session(exam=make_exam(instructor=self.user))
        upload = SimpleUploadedFile('wire.mp4', fake_video_bytes(b'wire'), content_type='video/mp4')
        serializer = VideoUploadSerializer(
            data={'session': str(session.id), 'file': upload},
            context={'request': request_for(self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    @staticmethod
    def _result_with_model():
        result = fake_analysis_result()
        result.metadata = dict(result.metadata)
        result.metadata['model'] = {'object': 'yolo11x.pt', 'pose': 'yolo11x-pose.pt'}
        return result

    def test_enqueue_defaults_sample_rate_to_pipeline_default_not_one(self):
        """M9: a job created without an explicit rate uses the truthful default (15)."""
        video = self._make_video()
        expected = AnalysisJob._meta.get_field('frame_sample_rate').default
        self.assertEqual(expected, 15)

        with mock.patch('apis.ai.analyze_video', return_value=self._result_with_model()):
            self.client.post(reverse('videos_analyze', kwargs={'pk': video.id}), {}, format='json')

        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.frame_sample_rate, expected)

    def test_client_sample_rate_is_honoured_and_wired_to_pipeline(self):
        """H2: a client-supplied rate is stored AND passed to analyze_video."""
        video = self._make_video()

        with mock.patch('apis.ai.analyze_video', return_value=self._result_with_model()) as mocked:
            self.client.post(
                reverse('videos_analyze', kwargs={'pk': video.id}),
                {'frame_sample_rate': 5}, format='json',
            )

        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.frame_sample_rate, 5)
        self.assertEqual(mocked.call_args.kwargs.get('sample_every_n'), 5)

    def test_invalid_sample_rate_falls_back_to_default(self):
        """A non-numeric or sub-1 rate must not break enqueue; it uses the default."""
        video = self._make_video()
        default = AnalysisJob._meta.get_field('frame_sample_rate').default

        with mock.patch('apis.ai.analyze_video', return_value=self._result_with_model()):
            self.client.post(
                reverse('videos_analyze', kwargs={'pk': video.id}),
                {'frame_sample_rate': 'not-a-number'}, format='json',
            )

        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.frame_sample_rate, default)

    def test_pipeline_stamps_ai_model_version_as_provenance(self):
        """H3: ai_model_version is filled from the model that actually ran."""
        video = self._make_video()

        with mock.patch('apis.ai.analyze_video', return_value=self._result_with_model()):
            self.client.post(reverse('videos_analyze', kwargs={'pk': video.id}), {}, format='json')

        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.ai_model_version, 'yolo11x.pt')

    def test_ai_model_version_is_not_a_client_input(self):
        """H3: a client cannot dictate ai_model_version; provenance wins."""
        video = self._make_video()

        with mock.patch('apis.ai.analyze_video', return_value=self._result_with_model()):
            self.client.post(
                reverse('videos_analyze', kwargs={'pk': video.id}),
                {'ai_model_version': 'totally-fake-model'}, format='json',
            )

        job = AnalysisJob.objects.get(session=video.session)
        self.assertEqual(job.ai_model_version, 'yolo11x.pt')


class _Arr(list):
    """List that quacks like an ultralytics tensor row (``.tolist()``)."""

    def tolist(self):
        return list(self)


class _FakeBox:
    """Minimal stand-in for one ultralytics detection box."""

    def __init__(self, xyxy, conf):
        self.xyxy = [_Arr(xyxy)]
        self.conf = [conf]


class ConsolidateEventsTrackingTests(APITestCase):
    """H11: distinct people are not merged when their boxes are degenerate.

    ``PoseAnalyzer`` falls back to ``bbox=(0,0,0,0)`` when it cannot match a
    person box to the keypoints. ``_bbox_center`` maps that to ``None``. The old
    ``_same_track`` returned ``True`` whenever either centre was ``None``, so two
    different students who both fell back to ``(0,0,0,0)`` collapsed into a single
    event — the report then showed fewer people and fewer events than reality.
    The fix keeps the missing-box temporal fallback (so one person's behaviour
    still sustains across frames) but refuses to merge two same-frame detections,
    which are necessarily different people.
    """

    @staticmethod
    def _det(ts, bbox, behavior=None, confidence=0.9):
        from apis.ai import config
        from apis.ai.detector import FrameDetection

        return FrameDetection(
            behavior_type=behavior or config.LOOKING_AWAY,
            confidence=confidence,
            bbox=bbox,
            frame_number=int(ts * 10),
            timestamp_sec=float(ts),
        )

    def test_same_track_requires_two_known_centres(self):
        from apis.ai.detector import _same_track

        a = (100.0, 100.0, 60.0)
        # Two unlocalisable detections must NOT be assumed to be one person.
        self.assertFalse(_same_track(None, None))
        self.assertFalse(_same_track(a, None))
        self.assertFalse(_same_track(None, a))
        # Known, close centres still match; known, far centres do not.
        self.assertTrue(_same_track(a, (105.0, 102.0, 60.0)))
        self.assertFalse(_same_track(a, (400.0, 100.0, 60.0)))

    def test_two_degenerate_people_same_frames_stay_two_events(self):
        """Two students with (0,0,0,0) boxes across two frames → two events."""
        from apis.ai.detector import consolidate_events

        zero = (0.0, 0.0, 0.0, 0.0)
        # Person A and person B both flagged at t=0 and t=3 (within the 5s merge
        # window; 3s span clears the 2s min-duration so the events survive).
        detections = [
            self._det(0.0, zero), self._det(0.0, zero),
            self._det(3.0, zero), self._det(3.0, zero),
        ]
        events = consolidate_events(detections)
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event.frame_count, 2)
            self.assertAlmostEqual(event.duration_sec, 3.0)

    def test_single_degenerate_person_still_merges_across_frames(self):
        """One person's degenerate-box behaviour sustains into a single event."""
        from apis.ai.detector import consolidate_events

        zero = (0.0, 0.0, 0.0, 0.0)
        detections = [self._det(t, zero) for t in (0.0, 2.0, 4.0)]
        events = consolidate_events(detections)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].frame_count, 3)

    def test_distinct_real_boxes_are_two_events(self):
        """Control: two well-separated boxes were (and remain) two events."""
        from apis.ai.detector import consolidate_events

        left = (100, 50, 160, 200)
        right = (400, 60, 470, 210)
        detections = [
            self._det(0.0, left), self._det(0.0, right),
            self._det(3.0, left), self._det(3.0, right),
        ]
        events = consolidate_events(detections)
        self.assertEqual(len(events), 2)


class FfmpegTimeoutTests(APITestCase):
    """M7: the ffmpeg re-encode is bounded so a wedged process can't hang a worker.

    The single ``subprocess.run`` (in ``_reencode_h264``) carries a timeout; the
    short evidence clips use the default, while the full annotated-video re-encode
    passes a larger, duration-scaled budget so a long-but-healthy transcode is not
    killed prematurely. A timeout is treated like any ffmpeg failure (returns
    ``False``), so the caller falls back to the mp4v output.
    """

    def test_reencode_timeout_scales_and_clamps(self):
        from apis.ai.detector import _reencode_timeout_for

        # Short/zero durations get the floor; long ones scale; huge ones clamp.
        self.assertEqual(_reencode_timeout_for(0), 120.0)
        self.assertEqual(_reencode_timeout_for(5), 120.0)       # 5*6=30 < floor
        self.assertEqual(_reencode_timeout_for(60), 360.0)      # 60*6, within range
        self.assertEqual(_reencode_timeout_for(100000), 1800.0)  # clamped to ceiling

    def test_reencode_forwards_timeout_to_subprocess(self):
        from apis.ai import face_tracker

        with mock.patch('apis.ai.face_tracker._ffmpeg_exe', return_value='ffmpeg'), \
                mock.patch('apis.ai.face_tracker.subprocess.run') as run, \
                mock.patch('apis.ai.face_tracker.os.path.exists', return_value=True), \
                mock.patch('apis.ai.face_tracker.os.path.getsize', return_value=10):
            run.return_value = SimpleNamespace(returncode=0)
            ok = face_tracker._reencode_h264('in.mp4', 'out.mp4', timeout=777)

        self.assertTrue(ok)
        self.assertEqual(run.call_args.kwargs.get('timeout'), 777)

    def test_reencode_timeout_is_treated_as_failure(self):
        import subprocess

        from apis.ai import face_tracker

        with mock.patch('apis.ai.face_tracker._ffmpeg_exe', return_value='ffmpeg'), \
                mock.patch(
                    'apis.ai.face_tracker.subprocess.run',
                    side_effect=subprocess.TimeoutExpired(cmd='ffmpeg', timeout=1),
                ):
            ok = face_tracker._reencode_h264('in.mp4', 'out.mp4', timeout=1)

        self.assertFalse(ok)  # caller keeps the mp4v file


class RawIntermediateCleanupTests(APITestCase):
    """M8: the ``.raw.mp4`` OpenCV intermediate is never orphaned on disk.

    Both the annotated-video writer (``detector._write_annotated_video``) and the
    clip writer (``face_tracker.extract_clip``) stage a mp4v ``.raw.mp4`` then
    re-encode it to H.264. Previously the intermediate was only deleted on the
    happy path; a crash (or M7 timeout treated as failure followed by an error)
    between writing and re-encoding left it behind. The lifecycle is now wrapped
    in try/finally so it is always removed or promoted onto the final path.
    """

    # The AI stack (cv2/numpy/ultralytics) is not installed in this environment,
    # so these tests inject a fake ``cv2`` module that the functions' lazy
    # ``import cv2`` picks up, plus a writer that lays down a real non-empty
    # ``.raw.mp4`` so the on-disk cleanup is genuinely observable.
    _PROP_FPS, _PROP_W, _PROP_H, _PROP_POS = 5, 3, 4, 1

    def _fake_cv2(self, write_error=None):
        import types

        props = {self._PROP_FPS: 30.0, self._PROP_W: 4, self._PROP_H: 4}

        class FakeCapture:
            def __init__(self, path):
                self._n = 0

            def isOpened(self):
                return True

            def get(self, prop):
                return props.get(prop, 0)

            def set(self, *a, **k):
                return True

            def read(self):
                # Grab a handful of frames then stop, so the clip writer always
                # records at least one frame regardless of loop bounds.
                self._n += 1
                return (True, object()) if self._n <= 3 else (False, None)

            def release(self):
                pass

        class FakeWriter:
            def __init__(self, path, *a, **k):
                with open(str(path), 'wb') as fh:  # non-empty raw intermediate
                    fh.write(b'\x00' * 1024)

            def isOpened(self):
                return True

            def write(self, frame):
                if write_error is not None:
                    raise write_error

            def release(self):
                pass

        module = types.ModuleType('cv2')
        module.CAP_PROP_FPS = self._PROP_FPS
        module.CAP_PROP_FRAME_WIDTH = self._PROP_W
        module.CAP_PROP_FRAME_HEIGHT = self._PROP_H
        module.CAP_PROP_POS_FRAMES = self._PROP_POS
        module.VideoWriter_fourcc = lambda *a: 0
        module.VideoCapture = FakeCapture
        module.VideoWriter = FakeWriter
        return module, FakeWriter

    def _media(self, name):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        out = Path(media_root) / name
        return out, Path(str(out) + '.raw.mp4')

    def test_annotated_video_crash_removes_raw(self):
        from apis.ai import detector

        fake_cv2, _ = self._fake_cv2()
        out, raw = self._media('annot.mp4')

        with mock.patch.dict('sys.modules', {'cv2': fake_cv2}), \
                mock.patch('apis.ai.detector.read_metadata',
                           return_value=SimpleNamespace(fps=30.0, width=4, height=4, duration_sec=1.0)), \
                mock.patch('apis.ai.face_tracker._reencode_h264', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                detector._write_annotated_video('video.mp4', [], [], str(out))

        self.assertFalse(raw.exists())  # M8: intermediate cleaned up on crash

    def test_annotated_video_without_ffmpeg_promotes_raw(self):
        from apis.ai import detector

        fake_cv2, _ = self._fake_cv2()
        out, raw = self._media('annot.mp4')

        with mock.patch.dict('sys.modules', {'cv2': fake_cv2}), \
                mock.patch('apis.ai.detector.read_metadata',
                           return_value=SimpleNamespace(fps=30.0, width=4, height=4, duration_sec=1.0)), \
                mock.patch('apis.ai.face_tracker._reencode_h264', return_value=False):
            result = detector._write_annotated_video('video.mp4', [], [], str(out))

        self.assertEqual(result, str(out))
        self.assertTrue(out.exists())   # raw promoted onto the final path
        self.assertFalse(raw.exists())  # no leftover intermediate

    def test_clip_crash_removes_raw(self):
        from apis.ai import face_tracker

        # The writer raises mid-loop (after the .raw.mp4 stub is created), the
        # exact crash window M8 protects — no dependency on the re-encode patch.
        fake_cv2, _ = self._fake_cv2(write_error=RuntimeError('boom'))
        out, raw = self._media('clip.mp4')

        with mock.patch.dict('sys.modules', {'cv2': fake_cv2}):
            with self.assertRaises(RuntimeError):
                face_tracker.extract_clip('video.mp4', 5.0, str(out))

        self.assertFalse(raw.exists())  # M8: clip intermediate cleaned up on crash


class YoloPersonBoxReuseTests(APITestCase):
    """H6: evidence reuses the YOLO person boxes instead of a Haar sweep.

    The pose pass already detects every person per frame; those boxes are
    harvested into a :class:`PersonIndex` and reused, so the video is not scanned
    a third time with the Haar cascade.
    """

    def test_pose_analyzer_surfaces_person_boxes_without_looking_away(self):
        """``analyze_frame`` returns person boxes even when nobody is flagged."""
        from apis.ai.pose_analyzer import PoseAnalyzer

        analyzer = PoseAnalyzer()
        # keypoints=None → no looking-away detection, but boxes still present.
        result = SimpleNamespace(
            boxes=[_FakeBox([10, 20, 60, 200], 0.91), _FakeBox([300, 30, 360, 210], 0.82)],
            keypoints=None,
        )
        analyzer._model = lambda frame, **kwargs: [result]

        detections, person_boxes = analyzer.analyze_frame(object())

        self.assertEqual(detections, [])  # nobody looking away
        self.assertEqual(len(person_boxes), 2)  # both persons still tracked
        self.assertEqual(person_boxes[0][0], (10.0, 20.0, 60.0, 200.0))
        # The backward-compatible wrapper still returns just the detections.
        self.assertEqual(analyzer.analyze(object()), [])

    def test_index_from_person_boxes_tracks_and_labels_left_to_right(self):
        """YOLO boxes build a queryable, left-to-right-labelled PersonIndex."""
        from apis.ai.face_tracker import index_from_person_boxes

        # Two people, each sighted in two consecutive sampled frames.
        frames = [
            (0.0, [(100, 50, 160, 200), (400, 60, 470, 210)]),
            (1.0, [(104, 52, 164, 202), (398, 58, 468, 208)]),
        ]
        index = index_from_person_boxes(frames, 640, 480)

        self.assertEqual(index.box_kind, 'person')
        self.assertEqual(index.person_count, 2)
        # person_1 is the leftmost track; person_2 the rightmost.
        self.assertEqual(set(index.persons_at(0.0)), {'person_1', 'person_2'})
        left_bbox = index.persons_at(0.0)['person_1']
        right_bbox = index.persons_at(0.0)['person_2']
        self.assertLess(left_bbox[0], right_bbox[0])

    def test_evidence_consumes_pipeline_index_and_skips_haar_sweep(self):
        """``_attach_alert_evidence`` uses the supplied index; never calls track_persons."""
        from apis import services

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)

        with override_settings(MEDIA_ROOT=media_root):
            user = make_user(username='h6', email='h6@example.com')
            session = make_session(exam=make_exam(instructor=user))
            video_path = Path(media_root) / 'h6.mp4'
            video_path.write_bytes(fake_video_bytes(b'h6'))
            video = SimpleNamespace(file=SimpleNamespace(path=str(video_path)))

            alert = Alert.objects.create(
                session=session,
                timestamp_sec=5,
                behavior_type=Alert.BehaviorType.LOOKING_AWAY,
                severity=Alert.Severity.MEDIUM,
                confidence_score=0.6,
                metadata={'source': 'ai-pipeline'},
            )

            # A YOLO-derived index: person boxes, queryable like the real one.
            person_index = SimpleNamespace(
                box_kind='person',
                query=lambda ts: ('person_2', (300.0, 30.0, 360.0, 210.0)),
                persons_at=lambda ts: {'person_2': (300.0, 30.0, 360.0, 210.0)},
            )

            with mock.patch('apis.ai.face_tracker.track_persons') as haar:
                services._attach_alert_evidence(
                    video, session, [alert], person_index=person_index,
                )

            haar.assert_not_called()  # H6: no redundant Haar sweep

        alert.refresh_from_db()
        self.assertEqual(alert.metadata.get('person_id'), 'person_2')
        # The flagged person's bbox (x, y, w, h) is recorded from the YOLO box.
        self.assertEqual(alert.metadata.get('bbox'), [300, 30, 60, 180])

    def test_build_ai_report_passes_person_index_into_evidence(self):
        """The pipeline result's ``person_index`` reaches the evidence layer (H6)."""
        from apis import services

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)

        with override_settings(MEDIA_ROOT=media_root):
            user = make_user(username='h6b', email='h6b@example.com')
            session = make_session(exam=make_exam(instructor=user))
            upload = SimpleUploadedFile('h6b.mp4', fake_video_bytes(b'h6b'), content_type='video/mp4')
            serializer = VideoUploadSerializer(
                data={'session': str(session.id), 'file': upload},
                context={'request': request_for(user)},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()

            sentinel_index = SimpleNamespace(box_kind='person', tracks={})
            result = fake_analysis_result()
            result.person_index = sentinel_index

            with mock.patch('apis.ai.analyze_video', return_value=result), \
                    mock.patch('apis.services._attach_alert_evidence') as attach:
                services.build_ai_report(session)

            self.assertIs(attach.call_args.kwargs.get('person_index'), sentinel_index)


class PersonCountFallbackTests(APITestCase):
    """M12: the report never fabricates "1 person" when attribution failed.

    ``person_count`` for the summary is derived from the ``person_id`` stamped
    onto each alert by ``_attach_alert_evidence``. If tracking/evidence fails
    entirely, no alert carries a ``person_id`` and the count is genuinely
    unknown — the old ``len(...) or 1`` fallback wrongly claimed one person was
    identified. The summary must instead say the count could not be determined.
    """

    def _build_report(self, attach_side_effect):
        from apis import services

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)

        with override_settings(MEDIA_ROOT=media_root):
            user = make_user(username='m12', email='m12@example.com')
            session = make_session(exam=make_exam(instructor=user))
            upload = SimpleUploadedFile(
                'm12.mp4', fake_video_bytes(b'm12'), content_type='video/mp4',
            )
            serializer = VideoUploadSerializer(
                data={'session': str(session.id), 'file': upload},
                context={'request': request_for(user)},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()

            with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()), \
                    mock.patch(
                        'apis.services._attach_alert_evidence',
                        side_effect=attach_side_effect,
                    ):
                return services.build_ai_report(session)

    def test_summary_is_unknown_when_no_person_attributed(self):
        # Evidence ran but stamped no person_id at all (tracking failed).
        report = self._build_report(lambda *a, **k: None)

        self.assertEqual(report.total_alerts, 1)
        self.assertIn('could not be determined', report.summary)
        self.assertNotIn('person(s).', report.summary)  # no fabricated count

    def test_summary_counts_distinct_attributed_people(self):
        def stamp_people(video, session, alerts, **kwargs):
            # Simulate evidence attributing each alert to its own person.
            for idx, alert in enumerate(alerts, start=1):
                alert.metadata = {**(alert.metadata or {}), 'person_id': f'person_{idx}'}

        report = self._build_report(stamp_people)

        self.assertEqual(report.total_alerts, 1)
        self.assertIn('across 1 person(s)', report.summary)


class IncompleteEvidenceSignalTests(APITestCase):
    """M1: partial evidence failures are recorded, not silently swallowed.

    ``_attach_alert_evidence`` processes alerts in a loop writing three artifacts
    each. If an extractor raises midway (e.g. disk fills), the old code left the
    artifact empty with no trace and — worse — an error outside the per-artifact
    guards could abort the loop, starving every later alert. Now each alert is
    isolated, failing artifacts are stamped onto ``metadata['evidence_incomplete']``
    /``evidence_errors``, and the count of affected alerts is returned and stamped
    onto the job so the partial state is visible.
    """

    def _make(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        user = make_user(username='m1', email='m1@example.com')
        session = make_session(exam=make_exam(instructor=user))
        video_path = Path(media_root) / 'm1.mp4'
        video_path.write_bytes(fake_video_bytes(b'm1'))
        video = SimpleNamespace(file=SimpleNamespace(path=str(video_path)))
        index = SimpleNamespace(
            box_kind='person',
            query=lambda ts: ('person_1', None),
            persons_at=lambda ts: {},
        )
        return media_root, session, video, index

    def _alert(self, session, ts):
        return Alert.objects.create(
            session=session,
            timestamp_sec=ts,
            behavior_type=Alert.BehaviorType.LOOKING_AWAY,
            severity=Alert.Severity.MEDIUM,
            confidence_score=0.6,
            metadata={'source': 'ai-pipeline'},
        )

    def test_failed_artifact_is_recorded_and_counted(self):
        from apis import services

        media_root, session, video, index = self._make()
        alert = self._alert(session, 5)

        with override_settings(MEDIA_ROOT=media_root), \
                mock.patch('apis.ai.face_tracker.extract_face_crop', side_effect=OSError('disk full')), \
                mock.patch('apis.ai.face_tracker.extract_annotated_frame', return_value=False), \
                mock.patch('apis.ai.face_tracker.extract_clip', return_value=False):
            incomplete = services._attach_alert_evidence(
                video, session, [alert], person_index=index,
            )

        self.assertEqual(incomplete, 1)
        alert.refresh_from_db()
        self.assertTrue(alert.metadata.get('evidence_incomplete'))
        self.assertIn('crop', alert.metadata.get('evidence_errors', []))

    def test_one_alert_failure_does_not_starve_the_others(self):
        """A raising extractor on alert #1 must not block alert #2's evidence."""
        from apis import services

        media_root, session, video, index = self._make()
        first = self._alert(session, 1)
        second = self._alert(session, 2)

        # extract_clip raises only for the first alert (matched by timestamp);
        # the snapshot extractor succeeds for both.
        def clip_side_effect(video_path, ts, out, **kwargs):
            if ts == 1:
                raise OSError('disk full')
            return True

        with override_settings(MEDIA_ROOT=media_root), \
                mock.patch('apis.ai.face_tracker.extract_face_crop', return_value=False), \
                mock.patch('apis.ai.face_tracker.extract_annotated_frame', return_value=True), \
                mock.patch('apis.ai.face_tracker.extract_clip', side_effect=clip_side_effect):
            incomplete = services._attach_alert_evidence(
                video, session, [first, second], person_index=index,
            )

        self.assertEqual(incomplete, 1)  # only the first alert
        first.refresh_from_db()
        second.refresh_from_db()
        # The first alert is flagged incomplete; the second still got its evidence.
        self.assertTrue(first.metadata.get('evidence_incomplete'))
        self.assertFalse(second.metadata.get('evidence_incomplete', False))
        self.assertTrue(second.clip_url)
        self.assertTrue(second.snapshot_url)

    def test_job_metadata_flags_incomplete_evidence(self):
        from apis import services

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)

        with override_settings(MEDIA_ROOT=media_root):
            user = make_user(username='m1b', email='m1b@example.com')
            session = make_session(exam=make_exam(instructor=user))
            upload = SimpleUploadedFile(
                'm1b.mp4', fake_video_bytes(b'm1b'), content_type='video/mp4',
            )
            serializer = VideoUploadSerializer(
                data={'session': str(session.id), 'file': upload},
                context={'request': request_for(user)},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()
            job = AnalysisJob.objects.create(session=session)

            with mock.patch('apis.ai.analyze_video', return_value=fake_analysis_result()), \
                    mock.patch('apis.services._attach_alert_evidence', return_value=2):
                services.build_ai_report(session, job=job)

        job.refresh_from_db()
        self.assertTrue(job.metadata.get('evidence_incomplete'))
        self.assertEqual(job.metadata.get('evidence_incomplete_count'), 2)


class ActivitySeriesQueryTests(APITestCase):
    """H7: the activity chart is two grouped queries, not 2·N per-day COUNTs.

    Verifies both correctness (counts land on the right days, out-of-window rows
    are excluded) and that the query count is a small constant independent of how
    many rows or days are involved.
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.user = make_user(username='chart', email='chart@example.com')
        self.exam = make_exam(instructor=self.user)

    @staticmethod
    def _days_ago(offset):
        from datetime import timedelta

        from django.utils import timezone

        return timezone.now() - timedelta(days=offset)

    def _video_on(self, day_offset):
        """Create a session + video uploaded *day_offset* days ago; return the session."""
        session = make_session(exam=self.exam, student_identifier=f'v-{uuid.uuid4().hex[:6]}')
        video = Video.objects.create(
            session=session,
            file=SimpleUploadedFile('v.mp4', fake_video_bytes(), content_type='video/mp4'),
            file_hash=uuid.uuid4().hex,
        )
        # uploaded_at is auto_now_add, so bypass it with an UPDATE.
        Video.objects.filter(pk=video.pk).update(uploaded_at=self._days_ago(day_offset))
        return session

    def _job_on(self, session, day_offset):
        job = AnalysisJob.objects.create(session=session)
        AnalysisJob.objects.filter(pk=job.pk).update(created_at=self._days_ago(day_offset))

    def test_series_counts_map_to_correct_days(self):
        from apis.services import activity_series_for

        # Uploads: two today, one two days ago, one outside the 7-day window.
        s_today_a = self._video_on(0)
        s_today_b = self._video_on(0)
        s_old = self._video_on(2)
        self._video_on(8)  # out of window — must be excluded
        # Analyses: one today, two one-day-ago.
        self._job_on(s_today_a, 0)
        self._job_on(s_today_b, 1)
        self._job_on(s_old, 1)

        series = activity_series_for(self.user)

        self.assertEqual(len(series['videos']), 7)
        self.assertEqual(len(series['analyses']), 7)
        # days run oldest -> newest: idx 6 = today, 5 = yesterday, 4 = two days ago.
        self.assertEqual(series['videos'][6], 2)
        self.assertEqual(series['videos'][4], 1)
        self.assertEqual(sum(series['videos']), 3)  # the 8-days-ago upload is dropped
        self.assertEqual(series['analyses'][6], 1)
        self.assertEqual(series['analyses'][5], 2)
        self.assertEqual(sum(series['analyses']), 3)

    def test_query_count_is_constant_regardless_of_volume(self):
        from apis.services import activity_series_for

        for _ in range(6):
            session = self._video_on(0)
            self._job_on(session, 0)

        # One grouped query per series (videos, analyses) — never 2·N.
        with self.assertNumQueries(2):
            activity_series_for(self.user)


class NotifyUsersEmailOffloadTests(APITestCase):
    """H9: notify_users writes in-app notices inline but offloads SMTP to the queue.

    A request that resolves many supervisors must not block on N sequential
    emails. The notifications stay synchronous; the email fan-out is enqueued
    (async) or sent inline only when the queue is eager (dev/test default).
    """

    @staticmethod
    def _make_users(n):
        return [make_user(username=f'sup{i}', email=f'sup{i}@example.com') for i in range(n)]

    def test_inapp_notifications_created_for_all_users(self):
        from apis.services import notify_users

        users = self._make_users(3)
        notify_users(users, Notification.NotifType.EXAM_UPDATED, 'Title', 'Body')

        for user in users:
            self.assertEqual(Notification.objects.filter(recipient=user).count(), 1)

    def test_eager_queue_sends_emails_inline(self):
        from django.core import mail

        from apis.services import notify_users

        users = self._make_users(3)
        notify_users(users, Notification.NotifType.EXAM_UPDATED, 'Subj', 'Body')

        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual({m.to[0] for m in mail.outbox}, {u.email for u in users})

    def test_async_queue_enqueues_email_job_and_skips_inline_send(self):
        from django.core import mail

        from apis.services import notify_users
        from apis.tasks import send_notification_emails

        users = self._make_users(2)
        fake_queue = mock.Mock()
        fake_queue.is_async = True

        with mock.patch('django_rq.get_queue', return_value=fake_queue):
            notify_users(users, Notification.NotifType.EXAM_CANCELLED, 'Subj', 'Body')

        # In-app notices are still written inline.
        self.assertEqual(Notification.objects.filter(recipient__in=users).count(), 2)
        # The SMTP fan-out was handed to the queue, not run on this thread.
        fake_queue.enqueue.assert_called_once()
        args = fake_queue.enqueue.call_args.args
        self.assertIs(args[0], send_notification_emails)
        self.assertEqual(set(args[1]), {u.email for u in users})
        self.assertEqual(args[2], 'Subj')
        self.assertEqual(len(mail.outbox), 0)  # nothing emailed on the HTTP thread

    def test_worker_sends_one_email_per_recipient_and_skips_blanks(self):
        from django.core import mail

        from apis.tasks import send_notification_emails

        sent = send_notification_emails(['a@example.com', '', 'b@example.com'], 'S', 'B')

        self.assertEqual(sent, 2)  # the blank address is skipped
        self.assertEqual({m.to[0] for m in mail.outbox}, {'a@example.com', 'b@example.com'})


class DeleteExpiredVideosCommandTests(TestCase):
    """M6: ``delete_expired_videos`` survives files it cannot delete.

    A stored video file held open (a worker mid-analysis, or a lingering
    handle on Windows) makes ``file.delete()`` raise ``OSError``. The command
    must keep that Video row (so the file is retried, not orphaned) and must
    not let one stuck file abort the sweep and strand every later expired
    video.
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _expired_video(self, student_identifier, content):
        from datetime import timedelta

        from django.utils import timezone

        session = make_session(student_identifier=student_identifier)
        return Video.objects.create(
            session=session,
            file=SimpleUploadedFile(
                f'{student_identifier}.mp4', content, content_type='video/mp4'),
            original_filename=f'{student_identifier}.mp4',
            file_hash=hashlib.sha256(content).hexdigest(),
            expires_at=timezone.now() - timedelta(days=1),
        )

    def test_deletes_expired_video_file_and_row(self):
        video = self._expired_video('gone', b'expired video bytes')
        stored_path = Path(self.media_root) / video.file.name
        self.assertTrue(stored_path.exists())

        call_command('delete_expired_videos', verbosity=0)

        self.assertFalse(Video.objects.filter(id=video.id).exists())
        self.assertFalse(stored_path.exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.ActionType.VIDEO_DELETED,
                target_resource=str(video.id),
            ).exists())

    def test_dry_run_deletes_nothing(self):
        video = self._expired_video('keep', b'still here bytes')
        stored_path = Path(self.media_root) / video.file.name

        call_command('delete_expired_videos', '--dry-run', verbosity=0)

        self.assertTrue(Video.objects.filter(id=video.id).exists())
        self.assertTrue(stored_path.exists())
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.ActionType.VIDEO_DELETED).exists())

    def test_locked_file_keeps_row_and_skips_audit(self):
        from django.core.files.storage import FileSystemStorage

        video = self._expired_video('locked', b'locked video bytes')

        with mock.patch.object(
            FileSystemStorage, 'delete', autospec=True,
            side_effect=OSError('file is in use'),
        ):
            call_command('delete_expired_videos', verbosity=0)

        # Row kept so the file is retried next run, not orphaned.
        self.assertTrue(Video.objects.filter(id=video.id).exists())
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.ActionType.VIDEO_DELETED).exists())

    def test_one_locked_file_does_not_strand_later_videos(self):
        from django.core.files.storage import FileSystemStorage

        self._expired_video('first', b'first video bytes')
        self._expired_video('second', b'second video bytes')

        original_delete = FileSystemStorage.delete
        state = {'calls': 0}

        def flaky_delete(self, name):
            state['calls'] += 1
            if state['calls'] == 1:
                raise OSError('file is in use')
            return original_delete(self, name)

        with mock.patch.object(
            FileSystemStorage, 'delete', autospec=True, side_effect=flaky_delete,
        ):
            call_command('delete_expired_videos', verbosity=0)

        # One stuck file is kept; the other is still deleted in the same run.
        self.assertEqual(Video.objects.count(), 1)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.ActionType.VIDEO_DELETED).count(), 1)


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


class UserLookupAPITests(APITestCase):
    """Invite-by-email modal: GET /api/users/lookup/?email= behaviour."""

    def setUp(self):
        self.dean = make_user(username='dean', email='dean@example.com', role=User.Role.DEAN)
        self.instructor = make_user(username='prof.smith', email='smith@example.com')
        self.instructor.first_name = 'Sarah'
        self.instructor.last_name = 'Smith'
        self.instructor.save(update_fields=['first_name', 'last_name'])

    def test_lookup_requires_dean(self):
        self.client.force_authenticate(self.instructor)

        response = self.client.get(reverse('users_lookup'), {'email': self.instructor.email})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_lookup_returns_404_for_unknown_email(self):
        self.client.force_authenticate(self.dean)

        response = self.client.get(reverse('users_lookup'), {'email': 'nobody@example.com'})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_lookup_returns_account_with_display_name(self):
        self.client.force_authenticate(self.dean)

        response = self.client.get(reverse('users_lookup'), {'email': 'SMITH@example.com'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], str(self.instructor.id))
        self.assertEqual(response.data['email'], self.instructor.email)
        self.assertEqual(response.data['username'], 'prof.smith')
        self.assertEqual(response.data['display_name'], 'Sarah Smith')
        self.assertEqual(response.data['role'], User.Role.INSTRUCTOR)

    def test_lookup_display_name_falls_back_to_username(self):
        self.client.force_authenticate(self.dean)
        plain = make_user(username='plainuser', email='plain@example.com')

        response = self.client.get(reverse('users_lookup'), {'email': plain.email})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['display_name'], 'plainuser')

    def test_lookup_returns_role_for_non_instructor(self):
        # A non-instructor account is still returned; the frontend shows the
        # "not an instructor" message based on the role.
        self.client.force_authenticate(self.dean)

        response = self.client.get(reverse('users_lookup'), {'email': self.dean.email})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['role'], User.Role.DEAN)

    def test_lookup_requires_email_param(self):
        self.client.force_authenticate(self.dean)

        response = self.client.get(reverse('users_lookup'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ExamListAPITests(APITestCase):
    """Invite modal exam picker: GET /api/exams/ scoping."""

    def setUp(self):
        self.dean = make_user(username='dean', email='dean@example.com', role=User.Role.DEAN)
        self.owner = make_user(username='owner', email='owner@example.com')
        self.other = make_user(username='other', email='other@example.com')
        self.exam_a = make_exam(instructor=self.owner, name='Owner Exam A')
        self.exam_b = make_exam(instructor=self.other, name='Other Exam B')

    def test_dean_sees_every_exam(self):
        self.client.force_authenticate(self.dean)

        response = self.client.get(reverse('exams_list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {row['name'] for row in response.data}
        self.assertEqual(names, {'Owner Exam A', 'Other Exam B'})

    def test_instructor_sees_only_their_own_exams(self):
        self.client.force_authenticate(self.owner)

        response = self.client.get(reverse('exams_list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {row['name'] for row in response.data}
        self.assertEqual(names, {'Owner Exam A'})

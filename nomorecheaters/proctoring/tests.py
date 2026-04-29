from __future__ import annotations

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import AnalysisResult, Video


User = get_user_model()


class InstructorVideoUploadTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()

        self.user = User.objects.create_user(
            email="instructor@example.com",
            password="password12345",
            name="Instructor Example",
            role="user",
        )
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_instructor_can_upload_exam_video(self):
        upload = SimpleUploadedFile(
            "exam-session.mp4",
            b"fake video bytes",
            content_type="video/mp4",
        )

        response = self.client.post(reverse("instructor_video_upload"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Video.objects.count(), 1)

        video = Video.objects.get()
        self.assertEqual(video.uploaded_by, self.user)
        self.assertEqual(video.original_filename, "exam-session.mp4")
        self.assertEqual(video.content_type, "video/mp4")
        self.assertEqual(video.size_bytes, len(b"fake video bytes"))
        self.assertIn("file_url", response.data)
        self.assertTrue(response.data["file_url"].endswith(video.file.url))

    def test_upload_rejects_non_video_file(self):
        upload = SimpleUploadedFile(
            "notes.txt",
            b"not a video",
            content_type="text/plain",
        )

        response = self.client.post(reverse("instructor_video_upload"), {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Video.objects.count(), 0)


class DashboardAnalyticsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="instructor@example.com",
            password="password12345",
            name="Instructor Example",
            role="user",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="password12345",
            name="Other Instructor",
            role="user",
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="password12345",
            name="Admin Example",
            role="admin",
        )

    def _create_video(self, *, user, status=Video.Status.COMPLETED, name="exam.mp4"):
        return Video.objects.create(
            uploaded_by=user,
            file=f"videos/{user.id}/{name}",
            sha256=f"{user.id}-{name}-{status}",
            original_filename=name,
            content_type="video/mp4",
            size_bytes=100,
            status=status,
        )

    def test_dashboard_analytics_scopes_to_authenticated_instructor(self):
        first = self._create_video(user=self.user, name="first.mp4")
        self._create_video(user=self.user, status=Video.Status.PROCESSING, name="second.mp4")
        other = self._create_video(user=self.other_user, name="other.mp4")

        AnalysisResult.objects.create(
            video=first,
            summary="Flagged",
            cheating_students=["Student 01", "Student 02"],
            details={"students_flagged": 2, "suspicion_score": 0.8},
        )
        AnalysisResult.objects.create(
            video=other,
            summary="Other flagged",
            cheating_students=["Student 03"],
            details={"students_flagged": 1, "suspicion_score": 0.7},
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("dashboard_analytics"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_exams"], 2)
        self.assertEqual(response.data["total_alerts"], 2)
        self.assertEqual(response.data["flagged_exams"], 1)
        self.assertEqual(response.data["processing_exams"], 1)
        self.assertEqual(response.data["average_alerts_per_exam"], 1.0)
        self.assertNotIn("total_users", response.data)

    def test_dashboard_analytics_admin_sees_all_users_and_exams(self):
        video = self._create_video(user=self.user, name="first.mp4")
        self._create_video(user=self.other_user, status=Video.Status.FAILED, name="other.mp4")
        AnalysisResult.objects.create(
            video=video,
            summary="Flagged",
            cheating_students=["Student 01"],
            details={"students_flagged": 1, "suspicion_score": 0.5},
        )

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("dashboard_analytics"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_exams"], 2)
        self.assertEqual(response.data["total_alerts"], 1)
        self.assertEqual(response.data["failed_exams"], 1)
        self.assertEqual(response.data["total_users"], User.objects.count())

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DashboardActivityView,
    DashboardAnalyticsView,
    DashboardStatsView,
    GlobalThresholdsView,
    HistoryView,
    InstructorVideoUploadView,
    ThresholdsView,
    VideoViewSet,
)

router = DefaultRouter()
router.register(r"videos", VideoViewSet, basename="videos")

urlpatterns = [
    path("", include(router.urls)),
    path("instructor/videos/upload/", InstructorVideoUploadView.as_view(), name="instructor_video_upload"),
    path("dashboard/analytics/", DashboardAnalyticsView.as_view(), name="dashboard_analytics"),
    path("dashboard/stats/", DashboardStatsView.as_view(), name="dashboard_stats"),
    path("dashboard/activity/", DashboardActivityView.as_view(), name="dashboard_activity"),
    path("history/", HistoryView.as_view(), name="history"),
    path("thresholds/", ThresholdsView.as_view(), name="thresholds"),
    path("thresholds/global/", GlobalThresholdsView.as_view(), name="thresholds_global"),
]

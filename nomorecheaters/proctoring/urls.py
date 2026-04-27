from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DashboardActivityView,
    DashboardStatsView,
    GlobalThresholdsView,
    HistoryView,
    ThresholdsView,
    VideoViewSet,
)

router = DefaultRouter()
router.register(r"videos", VideoViewSet, basename="videos")

urlpatterns = [
    path("", include(router.urls)),
    path("dashboard/stats/", DashboardStatsView.as_view(), name="dashboard_stats"),
    path("dashboard/activity/", DashboardActivityView.as_view(), name="dashboard_activity"),
    path("history/", HistoryView.as_view(), name="history"),
    path("thresholds/", ThresholdsView.as_view(), name="thresholds"),
    path("thresholds/global/", GlobalThresholdsView.as_view(), name="thresholds_global"),
]

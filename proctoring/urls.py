from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ReportViewSet, SystemSettingsView, VideoViewSet

router = DefaultRouter()
router.register(r"videos", VideoViewSet, basename="video")
router.register(r"reports", ReportViewSet, basename="report")

urlpatterns = [
    path("", include(router.urls)),
    path("settings/", SystemSettingsView.as_view(), name="system-settings"),
]

from django.urls import path

from .views import SystemLogListView, SystemMetricsView

urlpatterns = [
    path("system/logs/", SystemLogListView.as_view(), name="system_logs"),
    path("system/metrics/", SystemMetricsView.as_view(), name="system_metrics"),
]


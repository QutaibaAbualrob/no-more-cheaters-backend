
from django.urls import path

from .views import (
    AnalyzeVideoView,
    DashboardActivityView,
    DashboardStatsView,
    DeleteUserView,
    GlobalThresholdsView,
    HallManagementView,
    InstructorOversightView,
    MyPreferencesView,
    MyThresholdsView,
    SystemLogsView,
    SystemMetricsView,
    ThresholdsView,
    UserActivityView,
    UsersListView,
    VideoDetailView,
    VideoHistoryView,
    VideoListView,
    VideoUploadView,
)

urlpatterns = [
    path('', UsersListView.as_view(), name='users_list'),
    path('me/preferences/', MyPreferencesView.as_view(), name='my_preferences'),
    path('videos/', VideoListView.as_view(), name='videos_list'),
    path('videos/upload/', VideoUploadView.as_view(), name='videos_upload'),
    path('videos/<uuid:pk>/', VideoDetailView.as_view(), name='videos_detail'),
    path('videos/<uuid:pk>/analyze/', AnalyzeVideoView.as_view(), name='videos_analyze'),
    path('history/', VideoHistoryView.as_view(), name='videos_history'),
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard_stats'),
    path('dashboard/activity/', DashboardActivityView.as_view(), name='dashboard_activity'),
    path('thresholds/', ThresholdsView.as_view(), name='thresholds'),
    path('thresholds/me/', MyThresholdsView.as_view(), name='my_thresholds'),
    path('thresholds/global/', GlobalThresholdsView.as_view(), name='global_thresholds'),
    path('system/logs/', SystemLogsView.as_view(), name='system_logs'),
    path('system/metrics/', SystemMetricsView.as_view(), name='system_metrics'),
    path('dean/instructors/', InstructorOversightView.as_view(), name='dean_instructor_oversight'),
    path('dean/halls/', HallManagementView.as_view(), name='dean_hall_management'),
    path('<uuid:pk>/activity/', UserActivityView.as_view(), name='user_activity'),
    path('<uuid:pk>/', DeleteUserView.as_view(), name='delete_user'),
]

from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import RegisterInstructorView, TokenObtainPairWithAuditView

urlpatterns = [
    path("register/", RegisterInstructorView.as_view(), name="register"),
    path("token/", TokenObtainPairWithAuditView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]

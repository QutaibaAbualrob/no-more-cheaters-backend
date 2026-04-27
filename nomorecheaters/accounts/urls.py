from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    AdminUserViewSet,
    LoginView,
    MeView,
    PasswordForgotView,
    PasswordResetView,
    PasswordVerifyView,
    RegisterView,
    SelfUpdateView,
)

router = DefaultRouter()
router.register(r"users", AdminUserViewSet, basename="admin-users")

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/me/update/", SelfUpdateView.as_view(), name="me_update"),
    path("auth/password/forgot/", PasswordForgotView.as_view(), name="password_forgot"),
    path("auth/password/verify/", PasswordVerifyView.as_view(), name="password_verify"),
    path("auth/password/reset/", PasswordResetView.as_view(), name="password_reset"),
    path("", include(router.urls)),
]

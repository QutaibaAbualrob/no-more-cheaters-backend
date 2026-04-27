from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from audit.utils import log_event

from .permissions import IsAdminRole
from .models import PasswordResetCode
from .serializers import (
    AdminUserSerializer,
    AdminUserCreateUpdateSerializer,
    PasswordForgotSerializer,
    PasswordResetSerializer,
    PasswordVerifySerializer,
    RegisterSerializer,
    SelfUpdateSerializer,
    TokenObtainPairWithUserSerializer,
    UserSerializer,
)

User = get_user_model()


class RegisterView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        log_event(actor=user, level="info", message="User registered.", request=request)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    permission_classes = [permissions.AllowAny]
    serializer_class = TokenObtainPairWithUserSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        email = request.data.get("email") or ""
        user = User.objects.filter(email__iexact=email).first()
        if user:
            log_event(actor=user, level="info", message="User logged in.", request=request)
        return response


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class SelfUpdateView(APIView):
    def patch(self, request):
        serializer = SelfUpdateSerializer(instance=request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_event(actor=request.user, level="info", message="Profile updated.", request=request)
        return Response(UserSerializer(request.user).data)


def _build_activity_series(*, user, days: int = 14):
    end = timezone.localdate()
    start = end - timedelta(days=days - 1)

    date_list = [start + timedelta(days=idx) for idx in range(days)]
    date_keys = [d.isoformat() for d in date_list]

    from proctoring.models import AnalysisResult, Video

    videos_qs = Video.objects.filter(uploaded_by=user)
    analyses_qs = AnalysisResult.objects.filter(video__uploaded_by=user)

    video_counts = {
        row["day"]: row["count"]
        for row in videos_qs.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
    }
    analysis_counts = {
        row["day"]: row["count"]
        for row in analyses_qs.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
    }

    return {
        "days": date_keys,
        "videos": [int(video_counts.get(d, 0)) for d in date_list],
        "analyses": [int(analysis_counts.get(d, 0)) for d in date_list],
    }


class AdminUserViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminRole]
    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = AdminUserSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        if self.action in ("list", "retrieve"):
            return AdminUserSerializer
        return AdminUserCreateUpdateSerializer

    def perform_update(self, serializer):
        user = serializer.save()
        log_event(actor=self.request.user, level="warning", message=f"Admin updated user {user.email}.", request=self.request)

    def perform_destroy(self, instance):
        email = instance.email
        instance.delete()
        log_event(actor=self.request.user, level="warning", message=f"Admin deleted user {email}.", request=self.request)

    def create(self, request, *args, **kwargs):
        input_serializer = self.get_serializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        user = input_serializer.save()
        log_event(
            actor=self.request.user,
            level="warning",
            message=f"Admin created user {user.email}.",
            request=self.request,
        )
        output_serializer = AdminUserSerializer(user)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        input_serializer = self.get_serializer(instance, data=request.data, partial=True)
        input_serializer.is_valid(raise_exception=True)
        self.perform_update(input_serializer)
        instance.refresh_from_db()
        return Response(AdminUserSerializer(instance).data)

    @action(detail=True, methods=["get"], url_path="activity")
    def activity(self, request, pk=None):
        user = self.get_object()
        return Response(_build_activity_series(user=user, days=14))


class PasswordForgotView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordForgotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip()
        user = User.objects.filter(email__iexact=email).first()

        if not user:
            log_event(
                actor=None,
                level="warning",
                message="Password reset requested for unknown email.",
                request=request,
                meta={"email": email},
            )
            return Response(
                {
                    "ok": True,
                    "message": "If an account exists for this email, a verification code has been sent.",
                }
            )

        now = timezone.now()
        PasswordResetCode.objects.filter(email__iexact=email, used_at__isnull=True).update(used_at=now)

        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = now + timedelta(seconds=90)

        PasswordResetCode.objects.create(
            user=user,
            email=email,
            code_hash=make_password(code),
            expires_at=expires_at,
        )

        subject = "No More Cheaters — Password reset code"
        message = (
            f"Your password reset verification code is: {code}\n\n"
            "This code expires in 90 seconds.\n\n"
            "If you didn’t request this, you can ignore this email."
        )
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[email],
            fail_silently=False,
        )

        log_event(
            actor=user,
            level="warning",
            message="Password reset code requested.",
            request=request,
            meta={"email": email},
        )

        return Response(
            {
                "ok": True,
                "message": "If an account exists for this email, a verification code has been sent.",
            }
        )


class PasswordVerifyView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip()
        code = serializer.validated_data["code"].strip()

        item = (
            PasswordResetCode.objects.filter(email__iexact=email, used_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if not item:
            return Response({"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST)

        if item.is_expired:
            item.used_at = timezone.now()
            item.save(update_fields=["used_at"])
            return Response({"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST)

        if item.attempts >= 5:
            item.used_at = timezone.now()
            item.save(update_fields=["used_at"])
            return Response({"detail": "Too many attempts. Please request a new code."}, status=status.HTTP_400_BAD_REQUEST)

        if not check_password(code, item.code_hash):
            item.attempts += 1
            if item.attempts >= 5:
                item.used_at = timezone.now()
                item.save(update_fields=["attempts", "used_at"])
            else:
                item.save(update_fields=["attempts"])
            return Response({"detail": "Invalid or expired code."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"ok": True})


class PasswordResetView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip()
        code = serializer.validated_data["code"].strip()
        new_password = serializer.validated_data["new_password"]

        user = User.objects.filter(email__iexact=email).first()
        item = (
            PasswordResetCode.objects.filter(email__iexact=email, used_at__isnull=True)
            .order_by("-created_at")
            .first()
        )

        if not user or not item or item.is_expired:
            return Response({"detail": "Invalid email or code."}, status=status.HTTP_400_BAD_REQUEST)

        if item.attempts >= 5:
            item.used_at = timezone.now()
            item.save(update_fields=["used_at"])
            return Response({"detail": "Too many attempts. Please request a new code."}, status=status.HTTP_400_BAD_REQUEST)

        if not check_password(code, item.code_hash):
            item.attempts += 1
            if item.attempts >= 5:
                item.used_at = timezone.now()
                item.save(update_fields=["attempts", "used_at"])
            else:
                item.save(update_fields=["attempts"])
            return Response({"detail": "Invalid email or code."}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save(update_fields=["password"])

        now = timezone.now()
        PasswordResetCode.objects.filter(email__iexact=email, used_at__isnull=True).update(used_at=now)

        log_event(actor=user, level="warning", message="Password reset completed.", request=request)
        return Response({"ok": True})

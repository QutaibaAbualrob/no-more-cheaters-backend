from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from audit.utils import log_audit
from .serializers import RegisterInstructorSerializer


class RegisterInstructorView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not (
            settings.DEBUG or getattr(settings, "ALLOW_INSTRUCTOR_REGISTER", False)
        ):
            return Response(
                {"detail": "Registration is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ser = RegisterInstructorSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        log_audit(user, "register", "user", str(user.pk), request)
        return Response({"id": user.id, "username": user.username}, status=status.HTTP_201_CREATED)


class TokenObtainPairWithAuditView(TokenObtainPairView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            username = request.data.get("username")
            if username:
                try:
                    u = User.objects.get(username=username)
                    log_audit(u, "login", "auth", "", request)
                except User.DoesNotExist:
                    pass
        return response

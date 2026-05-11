from django.contrib.auth import get_user_model
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import generics

from .models import UserPreferences
from .serializers import (
    UserPreferencesReadSerializer,
    UserPreferencesUpdateSerializer,
    UserSerializer,
)


User = get_user_model()


class UsersListView(generics.ListAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class DeleteUserView(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class MyPreferencesView(APIView):
    """Retrieve or update preferences for the authenticated user.

    Preferences are created lazily on first access so new users always get
    sensible defaults without requiring a separate setup step.
    """

    def get(self, request):
        preferences, _created = UserPreferences.objects.get_or_create(user=request.user)
        serializer = UserPreferencesReadSerializer(preferences)
        return Response(serializer.data)

    def patch(self, request):
        preferences, _created = UserPreferences.objects.get_or_create(user=request.user)
        serializer = UserPreferencesUpdateSerializer(
            preferences,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserPreferencesReadSerializer(preferences).data)

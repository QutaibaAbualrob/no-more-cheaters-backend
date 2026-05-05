from django.contrib.auth import get_user_model
from rest_framework import generics

from .serializers import UserSerializer


User = get_user_model()


class UsersListView(generics.ListAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class DeleteUserView(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer

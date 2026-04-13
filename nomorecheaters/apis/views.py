
from rest_framework import generics


from django.views.generic import ListView
from django.contrib.auth.models import User

from .serializers import UserSerializer


# Create your views here.

class UsersListView(generics.ListAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
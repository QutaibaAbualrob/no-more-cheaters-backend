from rest_framework import generics
from accounts.permissions import IsAdminRole
from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditLogListView(generics.ListAPIView):
    permission_classes = [IsAdminRole]
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer

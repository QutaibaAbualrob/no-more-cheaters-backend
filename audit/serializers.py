from rest_framework import serializers
from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = (
            "id",
            "user",
            "action",
            "resource",
            "resource_id",
            "details",
            "ip_address",
            "created_at",
        )
        read_only_fields = fields

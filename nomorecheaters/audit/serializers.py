from __future__ import annotations

from rest_framework import serializers

from .models import SystemLog


class SystemLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor.email", read_only=True)

    class Meta:
        model = SystemLog
        fields = ("id", "created_at", "level", "message", "actor_email", "meta")
        read_only_fields = fields


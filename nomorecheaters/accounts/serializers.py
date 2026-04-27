from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "name", "institution", "role", "is_active", "date_joined", "last_login")
        read_only_fields = ("id", "role", "is_active", "date_joined", "last_login")


class AdminUserSerializer(serializers.ModelSerializer):
    videos_count = serializers.SerializerMethodField()
    analyses_count = serializers.SerializerMethodField()
    cheating_reports_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "name",
            "institution",
            "role",
            "is_active",
            "date_joined",
            "last_login",
            "videos_count",
            "analyses_count",
            "cheating_reports_count",
        )
        read_only_fields = ("id", "date_joined", "last_login", "videos_count", "analyses_count", "cheating_reports_count")

    def get_videos_count(self, obj) -> int:
        from proctoring.models import Video  # local import to avoid circulars

        return Video.objects.filter(uploaded_by=obj).count()

    def get_analyses_count(self, obj) -> int:
        from proctoring.models import AnalysisResult

        return AnalysisResult.objects.filter(video__uploaded_by=obj).count()

    def get_cheating_reports_count(self, obj) -> int:
        from proctoring.models import AnalysisResult

        return AnalysisResult.objects.filter(video__uploaded_by=obj).exclude(cheating_students=[]).count()


class AdminUserCreateUpdateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, min_length=8)

    class Meta:
        model = User
        fields = ("id", "email", "name", "institution", "role", "is_active", "password")
        read_only_fields = ("id",)

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def validate_email(self, value: str) -> str:
        qs = User.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That email is already registered.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User.objects.create_user(password=password, **validated_data)
        if not password:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)

        if password:
            instance.set_password(password)

        instance.save()
        return instance


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    name = serializers.CharField(max_length=150)
    institution = serializers.CharField(max_length=150, allow_blank=True, required=False)

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("That email is already registered.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data):
        email = validated_data["email"]
        password = validated_data["password"]
        name = validated_data["name"]
        institution = validated_data.get("institution", "")

        return User.objects.create_user(
            email=email,
            password=password,
            name=name,
            institution=institution,
            role="user",
        )


class SelfUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    institution = serializers.CharField(max_length=150, required=False, allow_blank=True)
    current_password = serializers.CharField(write_only=True, required=False)
    new_password = serializers.CharField(write_only=True, required=False, min_length=8)

    def validate(self, attrs):
        new_password = attrs.get("new_password")
        current_password = attrs.get("current_password")

        if new_password and not current_password:
            raise serializers.ValidationError({"current_password": "Current password is required to set a new password."})

        if new_password:
            validate_password(new_password)

        return attrs

    def update(self, instance, validated_data):
        for field in ("name", "institution"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])

        new_password = validated_data.get("new_password")
        current_password = validated_data.get("current_password")
        if new_password and current_password:
            if not instance.check_password(current_password):
                raise serializers.ValidationError({"current_password": "Current password is incorrect."})
            instance.set_password(new_password)

        instance.save()
        return instance


class TokenObtainPairWithUserSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = getattr(user, "role", "user")
        token["email"] = getattr(user, "email", "")
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class PasswordForgotSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=4, max_length=12)


class PasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=4, max_length=12)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_new_password(self, value: str) -> str:
        validate_password(value)
        return value

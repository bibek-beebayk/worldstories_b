from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from rest_framework import serializers


User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)

    def _build_unique_username(self, email):
        base_username = email.split("@")[0].strip().lower() or "user"
        username = base_username
        suffix = 1

        while User.objects.filter(username=username).exists():
            username = f"{base_username}{suffix}"
            suffix += 1

        return username

    def create(self, validated_data):
        email = validated_data["email"]
        password = validated_data["password"]
        username = self._build_unique_username(email)
        return User.objects.create_user(email=email, password=password, username=username)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        user = authenticate(
            request=self.context.get("request"),
            username=email,
            password=password,
        )

        if not user:
            raise serializers.ValidationError("Invalid email or password.")

        attrs["user"] = user
        return attrs


class OTPValidateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.IntegerField()


class OTPResendSerializer(serializers.Serializer):
    email = serializers.EmailField()

from rest_framework import viewsets
from django.contrib.auth import get_user_model
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.response import Response
from django.conf import settings
from rest_framework import status
from django.core.mail import send_mail
from django.db import transaction
from .models import OTP
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    OTPValidateSerializer,
    OTPResendSerializer,
)


User = get_user_model()


def get_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def build_unique_username(base_value: str) -> str:
    base_username = (base_value or "user").strip().lower()
    username = base_username
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f"{base_username}{suffix}"
        suffix += 1
    return username


class AuthenticationViewSet(viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = None  # Placeholder for actual serializer class
    permission_classes = [AllowAny]

    def _send_registration_otp(self, email: str, code: int):
        send_mail(
            subject="WorldStories OTP Verification",
            message=f"Your OTP code is {code}. It expires in 10 minutes.",
            from_email=getattr(settings, "EMAIL_HOST_USER", None),
            recipient_list=[email],
            fail_silently=False,
        )

    @action(detail=False, methods=["post"])
    def register(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()
        password = serializer.validated_data["password"]

        with transaction.atomic():
            user = User.objects.filter(email=email).first()

            if user and user.otp_verified:
                return Response(
                    {"message": "A user with this email already exists."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not user:
                user = serializer.create({"email": email, "password": password})
            else:
                user.set_password(password)
                user.save(update_fields=["password"])

            OTP.objects.filter(
                user=user, otp_type="Registration", is_used=False
            ).update(is_used=True)
            otp = OTP.create(user.id, "Registration")

        try:
            self._send_registration_otp(user.email, otp.otp)
        except Exception:
            return Response(
                {"message": "Could not send OTP email. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "otp_required": True,
                "email": user.email,
                "message": "OTP sent to your email.",
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="validate-otp")
    def validate_otp(self, request):
        serializer = OTPValidateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].lower()
        otp_input = serializer.validated_data["otp"]

        user = User.objects.filter(email=email).first()
        if not user:
            return Response(
                {"message": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        otp_obj = OTP.objects.filter(
            user=user, otp_type="Registration", otp=otp_input, is_used=False
        ).first()
        if not otp_obj:
            return Response(
                {"message": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST
            )

        if otp_obj.is_expired:
            otp_obj.is_used = True
            otp_obj.save(update_fields=["is_used"])
            return Response(
                {"message": "OTP expired. Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp_obj.is_used = True
        otp_obj.save(update_fields=["is_used"])
        user.otp_verified = True
        user.save(update_fields=["otp_verified"])

        tokens = get_tokens(user)
        return Response(
            {
                **tokens,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "username": user.username,
                },
            }
        )

    @action(detail=False, methods=["post"], url_path="resend-otp")
    def resend_otp(self, request):
        serializer = OTPResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = User.objects.filter(email=email).first()
        if not user:
            return Response(
                {"message": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if user.otp_verified:
            return Response(
                {"message": "User is already verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        OTP.objects.filter(user=user, otp_type="Registration", is_used=False).update(
            is_used=True
        )
        otp = OTP.create(user.id, "Registration")

        try:
            self._send_registration_otp(user.email, otp.otp)
        except Exception:
            return Response(
                {"message": "Could not send OTP email. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"message": "OTP resent successfully."})

    @action(detail=False, methods=["post"])
    def login(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        if not user.otp_verified:
            return Response(
                {"message": "Please verify your email with OTP before logging in."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tokens = get_tokens(user)
        return Response(
            {
                **tokens,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "username": user.username,
                },
            }
        )

    @action(detail=False, methods=["post"], url_path="google-login")
    def google_login(self, request):
        token = request.data.get("token")

        if not token:
            return Response({"error": "Token missing"}, status=400)

        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            # Verify token
            idinfo = id_token.verify_oauth2_token(
                token, google_requests.Request(), settings.GOOGLE_CLIENT_ID
            )

            email = idinfo["email"]
            name = idinfo.get("name", "")

        except Exception:
            return Response({"error": "Invalid Google token"}, status=400)

        # Create user if not exists
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "username": build_unique_username(email.split("@")[0]),
                "otp_verified": True,
            },
        )

        if not user.otp_verified:
            user.otp_verified = True
            user.save(update_fields=["otp_verified"])

        # Create JWT tokens
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": name or user.username,
                },
            }
        )

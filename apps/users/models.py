import random
from versatileimagefield.fields import VersatileImageField
import uuid
from django.conf import settings

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.dispatch import receiver

# from core.config import OTP_VALID_DURATION
from core.libs.models import TimeStampModel
from .managers import CustomUserManager
from core.libs.images import warm

OTP_VALID_DURATION = 600  # 10 minutes


GENDER_CHOICES = [("Male", "Male"), ("Female", "Female"), ("Other", "Other")]


OTP_TYPE_CHOICES = [
    ("Registration", "Registration"),
    ("Password Reset", "Password Reset"),
]


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    username = models.CharField(max_length=255, unique=True)

    date_joined = models.DateTimeField(default=timezone.now)
    last_login = models.DateTimeField(null=True, blank=True)
    login_count = models.BigIntegerField(default=0)

    is_staff = models.BooleanField(default=False)
    # is_active = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    otp_verified = models.BooleanField(default=False)

    # address_country = models.CharField(max_length=255)
    # address_state = models.CharField(max_length=255, null=True, blank=True)
    # address_city = models.CharField(max_length=255)
    # address_street1 = models.CharField(max_length=255, null=True, blank=True)
    # address_street2 = models.CharField(max_length=255, null=True, blank=True)
    # bio = models.TextField(null=True, blank=True)
    # occupation = models.CharField(max_length=255)
    # date_of_birth = models.DateField(null=True)
    # gender = models.CharField(max_length=16, choices=GENDER_CHOICES)
    # full_name = models.CharField(max_length=255)
    # first_name = models.CharField(max_length=255, blank=True, null=True)
    # middle_name = models.CharField(max_length=255, null=True, blank=True)
    # last_name = models.CharField(max_length=255, blank=True, null=True)
    # phone_number = models.CharField(max_length=255, unique=True, null=True)
    # profile_picture = VersatileImageField(
        # upload_to="users/profile_pics/"
    # )

    # SIZES = {
    #     "profile_picture": {
    #         "small": "thumbnail__320x180",
    #         "medium": "thumbnail__640x360",
    #     }
    # }

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def get_login_response(self):
        res = {
            "otp_verified": self.otp_verified,
            "email": self.email,
            "username": self.username
        }
        return res

    def first_login(self):
        if self.login_count == 1:
            return True
        return False

    def __str__(self):
        return self.email

    def generate_username(self):
        low = 1000
        high = 9999
        if self.full_name:
            first_name = self.full_name.split(" ")[0]
        elif self.email:
            first_name = self.email.split("@")[0]
        cleaned_first_name = "".join(filter(str.isalnum, first_name))
        match = True
        count = 0
        while match:
            random_number = random.randint(low, high)
            username = f"{cleaned_first_name.lower()}{random_number}"
            count += 1
            if count > (high - low):
                raise AssertionError(
                    "No unique usernames are available. Please Increase your limit."
                )
            if not User.objects.filter(username=username).exists():
                match = False
        return username

    # def save(self, *args, **kwargs):
    #     if not self.username:
    #         self.username = self.generate_username()
    #     super().save(*args, **kwargs)


class OTP(TimeStampModel):
    otp = models.IntegerField()
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otps")
    otp_type = models.CharField(
        max_length=255, choices=OTP_TYPE_CHOICES, null=True, blank=True
    )
    is_used = models.BooleanField(default=False)

    @property
    def is_expired(self):
        if (timezone.now() - self.created_at).seconds > OTP_VALID_DURATION:
            return True
        return False

    @classmethod
    def create(cls, user_id: int, type: str):
        otp_length = settings.OTP_LENGTH
        start = int("1" + "0"*(otp_length-1))
        end = int("9"*otp_length)
        if settings.DEBUG:
            code = int("1"*otp_length)
        else:
            code = random.randint(start, end)
        otp = cls.objects.create(otp=code, user_id=user_id, otp_type=type)
        return otp

    class Meta:
        ordering = ["-created_at"]


@receiver(models.signals.post_save, sender=User)
def warm_images(sender, instance, **kwargs):
    warm(instance)

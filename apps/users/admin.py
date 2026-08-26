from django.contrib import admin
from .models import User, OTP, UserLoginLocation


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    pass


@admin.register(OTP)
class OTPAdmin(admin.ModelAdmin):
    pass


@admin.register(UserLoginLocation)
class UserLoginLocationAdmin(admin.ModelAdmin):
    list_display = ("user", "country", "city", "ip_address", "created_at")
    list_filter = ("country",)
    search_fields = ("user__email", "user__username", "city", "ip_address")
    readonly_fields = ("user", "ip_address", "country", "country_code", "city", "created_at")

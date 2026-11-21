from django.urls import path

from .api import (
    NewsletterConfirmAPIView,
    NewsletterMeAPIView,
    NewsletterResubscribeAPIView,
    NewsletterSubscribeAPIView,
    NewsletterUnsubscribeAPIView,
    NewsletterUnsubscribeByEmailAPIView,
)

app_name = "newsletter"

urlpatterns = [
    path("subscribe/", NewsletterSubscribeAPIView.as_view(), name="subscribe"),
    path("confirm/", NewsletterConfirmAPIView.as_view(), name="confirm"),
    path(
        "unsubscribe-by-email/",
        NewsletterUnsubscribeByEmailAPIView.as_view(),
        name="unsubscribe-by-email",
    ),
    path(
        "unsubscribe/<str:token>/",
        NewsletterUnsubscribeAPIView.as_view(),
        name="unsubscribe",
    ),
    path(
        "unsubscribe/<str:token>/resubscribe/",
        NewsletterResubscribeAPIView.as_view(),
        name="resubscribe",
    ),
    path("me/", NewsletterMeAPIView.as_view(), name="me"),
]

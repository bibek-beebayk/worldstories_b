from django.urls import path

from .admin_api import (
    NewsletterAdminIssueDeliveriesAPIView,
    NewsletterAdminIssuesAPIView,
    NewsletterAdminPreviewAPIView,
    NewsletterAdminResendConfirmationAPIView,
    NewsletterAdminRetryFailedAPIView,
    NewsletterAdminStatsAPIView,
    NewsletterAdminSubscribersAPIView,
    NewsletterAdminSubscribersExportAPIView,
    NewsletterAdminTestSendAPIView,
    NewsletterAdminUnsubscribeAPIView,
)

app_name = "newsletter_admin"

urlpatterns = [
    path("stats/", NewsletterAdminStatsAPIView.as_view(), name="stats"),
    path("subscribers/", NewsletterAdminSubscribersAPIView.as_view(), name="subscribers"),
    path(
        "subscribers/export/",
        NewsletterAdminSubscribersExportAPIView.as_view(),
        name="subscribers-export",
    ),
    path(
        "subscribers/<int:pk>/resend-confirmation/",
        NewsletterAdminResendConfirmationAPIView.as_view(),
        name="subscriber-resend-confirmation",
    ),
    path(
        "subscribers/<int:pk>/unsubscribe/",
        NewsletterAdminUnsubscribeAPIView.as_view(),
        name="subscriber-unsubscribe",
    ),
    path("issues/", NewsletterAdminIssuesAPIView.as_view(), name="issues"),
    path(
        "issues/<int:pk>/deliveries/",
        NewsletterAdminIssueDeliveriesAPIView.as_view(),
        name="issue-deliveries",
    ),
    path(
        "issues/<int:pk>/retry-failed/",
        NewsletterAdminRetryFailedAPIView.as_view(),
        name="issue-retry-failed",
    ),
    path("test-send/", NewsletterAdminTestSendAPIView.as_view(), name="test-send"),
    path("preview/", NewsletterAdminPreviewAPIView.as_view(), name="preview"),
]

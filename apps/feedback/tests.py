import io
import shutil
import tempfile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APITestCase

from apps.users.models import User

from .models import Feedback

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="feedback-tests-")


class FeedbackTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="feedbackadmin@example.com", username="feedbackadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="feedbackreader@example.com", username="feedbackreader", password="test-password"
        )

    def _submit(self, **data):
        payload = {"category": "idea", "message": "Please add dark mode.", **data}
        return self.client.post(reverse("feedback-create"), payload, format="json")


class FeedbackSubmitTests(FeedbackTestBase):
    def test_signed_out_visitor_can_send_feedback(self):
        response = self._submit(email="visitor@example.com", rating=4, page_url="/library")
        self.assertEqual(response.status_code, 201)
        feedback = Feedback.objects.get()
        self.assertIsNone(feedback.user)
        self.assertEqual(feedback.email, "visitor@example.com")
        self.assertEqual(feedback.rating, 4)
        self.assertEqual(feedback.page_url, "/library")
        self.assertEqual(feedback.status, Feedback.STATUS_NEW)

    def test_signed_in_sender_is_linked_and_email_defaults_to_account(self):
        self.client.force_authenticate(self.reader)
        self.assertEqual(self._submit().status_code, 201)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.user, self.reader)
        self.assertEqual(feedback.email, self.reader.email)

    def test_rejects_empty_message_and_out_of_range_rating(self):
        self.assertEqual(self._submit(message="  ").status_code, 400)
        self.assertEqual(self._submit(rating=6).status_code, 400)
        self.assertEqual(self._submit(category="nonsense").status_code, 400)
        self.assertFalse(Feedback.objects.exists())

    def test_sender_cannot_set_triage_fields(self):
        self._submit(status="resolved", admin_notes="hacked")
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.status, Feedback.STATUS_NEW)
        self.assertEqual(feedback.admin_notes, "")

    def test_submissions_are_throttled(self):
        for _ in range(10):
            self.assertEqual(self._submit().status_code, 201)
        self.assertEqual(self._submit().status_code, 429)


def _png(name="shot.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": TEST_MEDIA_ROOT}},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class FeedbackImageTests(FeedbackTestBase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def _submit_multipart(self, images):
        payload = {"category": "bug", "message": "Button is cut off.", "images": images}
        return self.client.post(reverse("feedback-create"), payload, format="multipart")

    def test_images_are_attached_and_listed_for_admins(self):
        response = self._submit_multipart([_png("a.png"), _png("b.png")])
        self.assertEqual(response.status_code, 201, response.data)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.images.count(), 2)

        self.client.force_authenticate(self.admin)
        item = self.client.get(reverse("admin-feedback-list")).data["results"][0]
        self.assertEqual(len(item["images"]), 2)
        self.assertTrue(item["images"][0]["url"].endswith(".png"))

    def test_more_than_five_images_is_rejected(self):
        response = self._submit_multipart([_png(f"{i}.png") for i in range(6)])
        self.assertEqual(response.status_code, 400)
        self.assertIn("images", response.data)
        self.assertFalse(Feedback.objects.exists())

    def test_non_image_upload_is_rejected(self):
        bogus = SimpleUploadedFile("notes.png", b"not really an image", content_type="image/png")
        self.assertEqual(self._submit_multipart([bogus]).status_code, 400)
        self.assertFalse(Feedback.objects.exists())


class FeedbackAdminTests(FeedbackTestBase):
    def setUp(self):
        super().setUp()
        self.bug = Feedback.objects.create(category="bug", message="Audio stops on chapter 2.")
        self.idea = Feedback.objects.create(
            category="idea", message="Offline mode", status=Feedback.STATUS_RESOLVED
        )
        self.list_url = reverse("admin-feedback-list")

    def test_non_admins_cannot_read_feedback(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_admin_lists_and_filters_feedback_with_status_counts(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pagination"]["count"], 2)
        self.assertEqual(response.data["status_counts"], {"new": 1, "reviewed": 0, "resolved": 1})

        response = self.client.get(self.list_url, {"status": "new"})
        self.assertEqual([item["id"] for item in response.data["results"]], [self.bug.id])
        response = self.client.get(self.list_url, {"category": "idea"})
        self.assertEqual([item["id"] for item in response.data["results"]], [self.idea.id])
        response = self.client.get(self.list_url, {"search": "audio"})
        self.assertEqual([item["id"] for item in response.data["results"]], [self.bug.id])

    def test_admin_triages_but_cannot_rewrite_message(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-feedback-detail", kwargs={"pk": self.bug.pk})
        response = self.client.patch(
            url, {"status": "reviewed", "admin_notes": "Repro'd on iOS", "message": "changed"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.bug.refresh_from_db()
        self.assertEqual(self.bug.status, Feedback.STATUS_REVIEWED)
        self.assertEqual(self.bug.admin_notes, "Repro'd on iOS")
        self.assertEqual(self.bug.message, "Audio stops on chapter 2.")

    def test_admin_can_delete_feedback(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-feedback-detail", kwargs={"pk": self.bug.pk})
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(Feedback.objects.filter(pk=self.bug.pk).exists())

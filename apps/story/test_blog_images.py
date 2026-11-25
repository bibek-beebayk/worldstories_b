from io import BytesIO
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase

from apps.story.models import BlogImage

User = get_user_model()


def _make_image_bytes(
    fmt: str = "JPEG",
    size: tuple[int, int] = (800, 600),
    color=(120, 45, 90),
    exif_bytes: bytes | None = None,
) -> bytes:
    mode = "RGBA" if fmt == "PNG" and len(color) == 4 else "RGB"
    img = Image.new(mode, size, color)
    buf = BytesIO()
    save_kwargs = {}
    if exif_bytes is not None:
        save_kwargs["exif"] = exif_bytes
    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def _make_animated_gif_bytes(size: tuple[int, int] = (120, 80)) -> bytes:
    frame1 = Image.new("RGB", size, (255, 0, 0))
    frame2 = Image.new("RGB", size, (0, 0, 255))
    buf = BytesIO()
    frame1.save(buf, format="GIF", save_all=True, append_images=[frame2], duration=100, loop=0)
    return buf.getvalue()


class BlogImageUploadTests(APITestCase):
    def setUp(self):
        self.media_dir = tempfile.mkdtemp(prefix="ws_blog_images_test_")
        self.addCleanup(shutil.rmtree, self.media_dir, True)
        self.override = override_settings(
            MEDIA_ROOT=self.media_dir,
            STORAGES={
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
            },
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.admin = User.objects.create_superuser(
            email="blogimg-admin@example.com",
            username="blogimg_admin",
            password="password123",
        )
        self.reader = User.objects.create_user(
            email="blogimg-reader@example.com",
            username="blogimg_reader",
            password="password123",
        )

    def test_valid_jpeg_becomes_webp_and_returns_expected_shape(self):
        self.client.force_authenticate(user=self.admin)
        data = _make_image_bytes("JPEG", (1200, 800))
        upload = SimpleUploadedFile("photo.jpg", data, content_type="image/jpeg")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIn("id", res.data)
        self.assertIn("url", res.data)
        self.assertTrue(res.data["url"].startswith("http"))
        self.assertTrue(res.data["url"].endswith(".webp"))
        self.assertEqual(res.data["width"], 1200)
        self.assertEqual(res.data["height"], 800)
        self.assertGreater(res.data["size_bytes"], 0)

        record = BlogImage.objects.get(pk=res.data["id"])
        self.assertEqual(record.uploaded_by_id, self.admin.id)
        self.assertEqual(record.original_name, "photo.jpg")
        with Image.open(record.image.path) as saved:
            self.assertEqual(saved.format, "WEBP")
            self.assertEqual(saved.size, (1200, 800))

    def test_large_png_is_resized_to_1600_width_webp(self):
        self.client.force_authenticate(user=self.admin)
        data = _make_image_bytes("PNG", (2400, 1200), (10, 20, 30, 200))
        upload = SimpleUploadedFile("wide.png", data, content_type="image/png")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["width"], 1600)
        self.assertEqual(res.data["height"], 800)
        self.assertTrue(res.data["url"].endswith(".webp"))

    def test_animated_gif_is_kept_as_gif(self):
        self.client.force_authenticate(user=self.admin)
        data = _make_animated_gif_bytes((120, 80))
        upload = SimpleUploadedFile("anim.gif", data, content_type="image/gif")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["url"].endswith(".gif"))
        self.assertEqual(res.data["width"], 120)
        self.assertEqual(res.data["height"], 80)

    def test_non_image_bytes_named_jpg_are_rejected(self):
        self.client.force_authenticate(user=self.admin)
        upload = SimpleUploadedFile("fake.jpg", b"not an image at all", content_type="image/jpeg")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.data)
        self.assertEqual(BlogImage.objects.count(), 0)

    def test_file_over_8mb_is_rejected(self):
        self.client.force_authenticate(user=self.admin)
        big_bytes = b"\xff" * (8 * 1024 * 1024 + 1)
        upload = SimpleUploadedFile("huge.jpg", big_bytes, content_type="image/jpeg")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("8 MB", res.data["error"])

    def test_dimensions_over_8000px_are_rejected(self):
        self.client.force_authenticate(user=self.admin)
        data = _make_image_bytes("PNG", (8001, 10))
        upload = SimpleUploadedFile("too-wide.png", data, content_type="image/png")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("8000", res.data["error"])

    def test_exif_orientation_applied_and_gps_stripped(self):
        self.client.force_authenticate(user=self.admin)
        exif = Image.Exif()
        exif[274] = 6  # Orientation = Rotate 90 CW (400x200 becomes 200x400)
        exif[34853] = {1: "N", 2: (27.0, 42.0, 0.0)}  # GPSInfo
        data = _make_image_bytes("JPEG", (400, 200), exif_bytes=exif.tobytes())
        upload = SimpleUploadedFile("phone.jpg", data, content_type="image/jpeg")

        res = self.client.post("/api/admin/blog-images/", {"image": upload}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["width"], 200)
        self.assertEqual(res.data["height"], 400)

        record = BlogImage.objects.get(pk=res.data["id"])
        with Image.open(record.image.path) as saved:
            self.assertEqual(saved.size, (200, 400))
            saved_exif = saved.getexif()
            self.assertNotIn(34853, saved_exif)
            self.assertEqual(len(saved_exif), 0)

    def test_non_admin_gets_403_and_anonymous_gets_401_or_403(self):
        data = _make_image_bytes("JPEG", (200, 200))

        # Anonymous
        upload_anon = SimpleUploadedFile("a.jpg", data, content_type="image/jpeg")
        res_anon = self.client.post("/api/admin/blog-images/", {"image": upload_anon}, format="multipart")
        self.assertIn(res_anon.status_code, (401, 403))

        # Non-admin authenticated user
        self.client.force_authenticate(user=self.reader)
        upload_user = SimpleUploadedFile("u.jpg", data, content_type="image/jpeg")
        res_user = self.client.post("/api/admin/blog-images/", {"image": upload_user}, format="multipart")
        self.assertEqual(res_user.status_code, 403)

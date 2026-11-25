"""Admin image upload endpoint for inline blog post images."""

from io import BytesIO
import os
import uuid

from django.core.files.base import ContentFile
from django.utils.text import slugify
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.story.models import BlogImage
from apps.story.api import IsSuperUser

MAX_BLOG_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB
MAX_BLOG_IMAGE_DIMENSION = 8000
MAX_OUTPUT_WIDTH = 1600
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


class BlogImageValidationError(Exception):
    """Plain-language validation error returned directly to the admin UI."""


def process_blog_image_upload(uploaded_file) -> tuple[bytes, str, int, int]:
    """Validate and optimize an uploaded image for blog post bodies.

    Returns ``(output_bytes, extension, width, height)``.
    Raises ``BlogImageValidationError`` with a user-facing message on failure.
    """
    if uploaded_file is None:
        raise BlogImageValidationError("Please choose an image file to upload.")

    file_size = getattr(uploaded_file, "size", None)
    if file_size is not None and file_size > MAX_BLOG_IMAGE_BYTES:
        raise BlogImageValidationError("Image is too large. Maximum file size is 8 MB.")

    raw_bytes = uploaded_file.read()
    if not raw_bytes:
        raise BlogImageValidationError("The uploaded file is empty.")
    if len(raw_bytes) > MAX_BLOG_IMAGE_BYTES:
        raise BlogImageValidationError("Image is too large. Maximum file size is 8 MB.")

    # Never trust file extension or Content-Type; inspect actual image bytes via Pillow.
    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img_format = (img.format or "").upper()
            if img_format not in ALLOWED_FORMATS:
                raise BlogImageValidationError(
                    "Unsupported image format. Please upload a JPG, PNG, WebP, or GIF image."
                )

            width, height = img.size
            if width <= 0 or height <= 0:
                raise BlogImageValidationError("Could not read image dimensions.")
            if width > MAX_BLOG_IMAGE_DIMENSION or height > MAX_BLOG_IMAGE_DIMENSION:
                raise BlogImageValidationError(
                    "Image dimensions are too large. Maximum size is 8000 x 8000 pixels."
                )

            is_animated_gif = (
                img_format == "GIF"
                and bool(getattr(img, "is_animated", False))
                and int(getattr(img, "n_frames", 1)) > 1
            )
            if is_animated_gif:
                # Force decoding first frame to confirm the GIF isn't truncated/corrupt.
                img.load()
                return raw_bytes, "gif", width, height

            img.load()
            transposed = ImageOps.exif_transpose(img)
            # Re-check after transpose in case EXIF swapped axes (still <= 8000 either way).
            has_alpha = transposed.mode in ("RGBA", "LA") or (
                transposed.mode == "P" and "transparency" in transposed.info
            )
            converted = transposed.convert("RGBA" if has_alpha else "RGB")

            if converted.width > MAX_OUTPUT_WIDTH:
                new_height = max(1, round(converted.height * (MAX_OUTPUT_WIDTH / converted.width)))
                converted = converted.resize((MAX_OUTPUT_WIDTH, new_height), Image.Resampling.LANCZOS)

            out = BytesIO()
            # Saving without exif/info strips EXIF, GPS, and camera metadata.
            converted.save(out, format="WEBP", quality=82, method=6)
            out_bytes = out.getvalue()
            return out_bytes, "webp", converted.width, converted.height
    except BlogImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise BlogImageValidationError(
            "Unsupported or invalid image file. Please upload a JPG, PNG, WebP, or GIF image."
        )


def _safe_filename(original_name: str, ext: str) -> str:
    stem = os.path.splitext(os.path.basename(original_name or "image"))[0]
    cleaned = slugify(stem)[:60] or "image"
    return f"{cleaned}-{uuid.uuid4().hex[:8]}.{ext}"


class BlogImageUploadAPIView(APIView):
    """POST /api/admin/blog-images/ — upload and optimize an inline blog image."""

    permission_classes = [IsSuperUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploaded = request.FILES.get("image")
        try:
            out_bytes, ext, width, height = process_blog_image_upload(uploaded)
        except BlogImageValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        original_name = os.path.basename(getattr(uploaded, "name", "") or "")[:255]
        filename = _safe_filename(original_name, ext)

        blog_image = BlogImage(
            width=width,
            height=height,
            size_bytes=len(out_bytes),
            original_name=original_name,
            uploaded_by=request.user if request.user.is_authenticated else None,
        )
        blog_image.image.save(filename, ContentFile(out_bytes), save=True)

        url = request.build_absolute_uri(blog_image.image.url)
        return Response(
            {
                "id": blog_image.id,
                "url": url,
                "width": blog_image.width,
                "height": blog_image.height,
                "size_bytes": blog_image.size_bytes,
            },
            status=status.HTTP_201_CREATED,
        )

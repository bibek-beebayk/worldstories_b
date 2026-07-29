# Email backend
import os

from core.settings.base import BASE_DIR, MIDDLEWARE

DEBUG = False

ALLOWED_HOSTS = ["worldstories-b-production.up.railway.app"]

CSRF_TRUSTED_ORIGINS = [
    "https://worldstories-b-production.up.railway.app", "https://worldstories-f.netlify.app", "http://localhost:8080", "http://127.0.0.1:8080", "https://worldstories.net", "https://www.worldstories.net"
]

CORS_ALLOWED_ORIGINS = [
    "https://worldstories-b-production.up.railway.app", "https://worldstories-f.netlify.app", "http://localhost:8080", "http://127.0.0.1:8080", "https://worldstories.net", "https://www.worldstories.net"
]

CORS_ORIGIN_WHITELIST = [
    "https://worldstories-b-production.up.railway.app", "https://worldstories-f.netlify.app", "http://localhost:8080", "http://127.0.0.1:8080", "https://worldstories.net", "https://www.worldstories.net"
]

MIDDLEWARE += [
    "whitenoise.middleware.WhiteNoiseMiddleware",
]
STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"

STATIC_ROOT = os.path.join(BASE_DIR, "static")


# logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "file": {
            "level": "DEBUG",
            "class": "logging.FileHandler",
            "filename": "./debug.log",
        },
    },
    "loggers": {
        "": {  # empty string
            "handlers": ["file"],
            "level": "DEBUG",
            "propagate": True,
            },
    },
}

DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

# Cloudflare R2 settings
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
AWS_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", ""))
AWS_SECRET_ACCESS_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY",
    os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
)
AWS_STORAGE_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", os.environ.get("AWS_STORAGE_BUCKET_NAME", ""))
AWS_S3_ENDPOINT_URL = os.environ.get(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else "",
)
AWS_S3_REGION_NAME = "auto"
AWS_S3_SIGNATURE_VERSION = "s3v4"
AWS_S3_ADDRESSING_STYLE = "path"
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = False
AWS_S3_FILE_OVERWRITE = False

R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
AWS_S3_CUSTOM_DOMAIN = os.environ.get("AWS_S3_CUSTOM_DOMAIN", "")
if not AWS_S3_CUSTOM_DOMAIN and R2_PUBLIC_BASE_URL:
    AWS_S3_CUSTOM_DOMAIN = (
        R2_PUBLIC_BASE_URL.replace("https://", "").replace("http://", "").strip("/")
    )
if AWS_S3_CUSTOM_DOMAIN:
    AWS_S3_URL_PROTOCOL = "https:"
    MEDIA_URL = f"{AWS_S3_URL_PROTOCOL}//{AWS_S3_CUSTOM_DOMAIN}/"
elif R2_PUBLIC_BASE_URL:
    MEDIA_URL = f"{R2_PUBLIC_BASE_URL}/"

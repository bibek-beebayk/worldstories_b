# Email backend
import os

from core.settings.base import BASE_DIR, MIDDLEWARE

DEBUG = False

ALLOWED_HOSTS = ["worldstories-b-production.up.railway.app"]

# HTTPS / cookie hardening. Railway terminates TLS at a proxy in front of the
# app and forwards plain HTTP internally, so SECURE_PROXY_SSL_HEADER must
# come first — without it, Django can't tell a real HTTPS request from a
# plain HTTP one, and SECURE_SSL_REDIRECT/SESSION_COOKIE_SECURE below would
# either redirect-loop real HTTPS traffic or silently refuse to set cookies.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Starting conservative (1 hour) rather than the usual 1-year production
# value — HSTS makes browsers refuse plain HTTP for the whole duration, so
# it's safer to confirm HTTPS holds up under real traffic before raising
# this. Bump toward 31536000 (1 year) once that's confirmed.
SECURE_HSTS_SECONDS = 3600

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
# Logs to stdout at WARNING+ rather than a local DEBUG-level file: Railway
# captures container stdout natively, so a FileHandler here just grows
# unbounded on the ephemeral filesystem for no offsetting benefit — and
# DEBUG-level logging in production risks writing request bodies/tokens to
# disk.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "level": "WARNING",
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
        "": {  # empty string
            "handlers": ["console"],
            "level": "WARNING",
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

# Email backend
import os
from core.settings.base import BASE_DIR, MIDDLEWARE

SECRET_KEY = "django-insecure-mrk3h^2+7cas%6q$(6_gt!tixkx)=-)=m)l)id-c1qbm&r2df_"

DEBUG = False

ALLOWED_HOSTS = ["worldstories-b-production.up.railway.app"]

CSRF_TRUSTED_ORIGINS = [
    "https://worldstories-b-production.up.railway.app", "https://worldstories-f.netlify.app",
]

CORS_ALLOWED_ORIGINS = [
    "https://worldstories-b-production.up.railway.app", "https://worldstories-f.netlify.app"
]

CORS_ORIGIN_WHITELIST = [
    "https://worldstories-b-production.up.railway.app", "https://worldstories-f.netlify.app"
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
if R2_PUBLIC_BASE_URL:
    MEDIA_URL = f"{R2_PUBLIC_BASE_URL}/"

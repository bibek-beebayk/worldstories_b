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
# ACCOUNT_ID = "ffae1fbd17549c7fc17547a2777a43b0"
# AWS_ACCESS_KEY_ID = "9f95336e5293f68a42eea75b8cdde291"
# AWS_SECRET_ACCESS_KEY = (
#     "a9efe9365301ebc16d2fb91c3348db42776bc2810ab7b7a85bd302c5d4a49f9a"
# )
# AWS_STORAGE_BUCKET_NAME = "alnoor"
# AWS_S3_ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
# AWS_S3_SIGNATURE_VERSION = "s3v4"

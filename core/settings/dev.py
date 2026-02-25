import os
from .base import BASE_DIR

SECRET_KEY = 'django-insecure-mrk3h^2+7cas%6q$(7_gt!tixkx)=-)=m)l)id-c1qbm&r2df_'

DEBUG = True

ALLOWED_HOSTS = ["*"]

# Email backend
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

MEDIA_URL =  'media/'
MEDIA_ROOT =  BASE_DIR / 'media'

CSRF_TRUSTED_ORIGINS = [
    "https://fetunnel.worldstories.net",
    "https://betunnel.worldstories.net",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]
CORS_ALLOWED_ORIGINS = [
    "https://fetunnel.worldstories.net",
    "https://betunnel.worldstories.net",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]

API_BASE = "http://localhost:8000/api/v1"

# logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'file': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': './debug.log',
        },
    },
    'loggers': {
        '': { # empty string
            'handlers': ['file'],
            'level': 'DEBUG',
            'propagate': True,
        },
    },
}

DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

# Cloudflare R2 settings
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "b46a64eb384beb50a5fc80946bc0abc7")
AWS_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "1f8bcff6b47d61799db7beaae02a3fa4"))
AWS_SECRET_ACCESS_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY",
    os.environ.get("AWS_SECRET_ACCESS_KEY", "78e32042ccce9896aa5b5767338949146eda472fd532328302be87d2b0ed5260"),
)
AWS_STORAGE_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", os.environ.get("AWS_STORAGE_BUCKET_NAME", "worldstories"))
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

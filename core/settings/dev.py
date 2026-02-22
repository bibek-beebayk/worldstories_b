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


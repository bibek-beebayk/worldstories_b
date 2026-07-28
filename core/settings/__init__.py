from .base import *

try:
    from .env import *
except ImportError:
    if os.environ.get("DJANGO_ENV", "production").lower() in {
        "dev",
        "development",
        "local",
    }:
        from .dev import *
    else:
        from .prod import *

"""Encrypted model fields for platform OAuth credentials.

Worldstories has no existing encrypted-fields utility (checked
``core/libs/`` — it provides ``TimeStampModel``/``SingletonModel``,
pagination, image and admin helpers only), so this module is *new* for this
feature rather than a reuse of an existing project utility.

``django-encrypted-model-fields`` is the library named in the spec, but it
pins an old ``cryptography`` and has not been released against Django 5.x;
this project already ships ``cryptography`` (a transitive dependency of the
existing stack), so we implement the equivalent with Fernet directly. The
storage format is identical in spirit: the ciphertext is a urlsafe-base64
Fernet token kept in a ``text`` column.

Key management
--------------
The key comes from ``settings.SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY`` (env
var of the same name). It must be a urlsafe-base64 32-byte Fernet key::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If the key is missing we refuse to encrypt/decrypt rather than silently
falling back to plaintext — platform access tokens must never be stored in
the clear (spec 3.3).
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = getattr(settings, "SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        raise ImproperlyConfigured(
            "SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY is not set. Generate one "
            "with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    if isinstance(key, str):
        key = key.encode()
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:  # pragma: no cover - config error
        raise ImproperlyConfigured(
            "SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY must be a urlsafe-base64 "
            "encoded 32-byte key."
        ) from exc
    return Fernet(key)


class EncryptedTextField(models.TextField):
    """A ``TextField`` whose value is Fernet-encrypted at rest.

    Values are decrypted transparently on load. Because the ciphertext is
    randomised (Fernet embeds an IV and a timestamp), these columns cannot be
    filtered on or indexed — that is fine here, tokens are only ever read
    back by primary key through their ``PlatformConnection``.
    """

    # Fernet tokens are ~1.4x the plaintext plus 57 bytes of envelope; text
    # has no length limit so nothing to size here.

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        if isinstance(value, str):
            value = value.encode()
        return _fernet().encrypt(value).decode()

    def from_db_value(self, value, expression, connection):
        return self._decrypt(value)

    def to_python(self, value):
        # Called on deserialization/form cleaning. Values that are already
        # plaintext (e.g. assigned in Python before a save) pass through.
        return self._decrypt(value)

    @staticmethod
    def _decrypt(value):
        if value is None or value == "":
            return value
        if isinstance(value, str):
            raw = value.encode()
        else:
            raw = value
        try:
            return _fernet().decrypt(raw).decode()
        except (InvalidToken, base64.binascii.Error, TypeError):
            # Not a token we wrote (plaintext assigned in memory, or a row
            # written before the key rotated). Return as-is so a rotation
            # mistake surfaces as a failing API call rather than a 500 on
            # every queryset evaluation.
            return value if isinstance(value, str) else raw.decode(errors="replace")

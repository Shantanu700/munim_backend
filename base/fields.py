"""Model fields whose column holds ciphertext and whose attribute holds cleartext.

Why a field rather than an encrypt/decrypt service: every existing reader wants a
str on the attribute -- payments/razorpay.py builds a Basic auth header out of an
f-string, twin's webhook view calls .encode() on the secret, and
Merchant.is_payment_ready just tests it for truth. A custom field is the only
place to put encryption that leaves all three of them untouched.

The threat model, stated honestly so nobody over-trusts this. It defends database
backups, read replicas, a stolen pg_dump, and the query log. It does not defend a
compromised host: the key is in the same .env the process reads. That is still the
difference between "someone got a dump" and "someone got every merchant's Razorpay
secret".
"""
from __future__ import annotations

import math
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.core.validators import BaseValidator
from django.db import models

_OVERHEAD = 1 + 8 + 16 + 32
_BLOCK = 16

TOKEN_PREFIX = "gAAAAA"


def token_length(plaintext_bytes: int) -> int:
    """Exact column width a token over `plaintext_bytes` UTF-8 bytes needs."""
    raw = _OVERHEAD + _BLOCK * (plaintext_bytes // _BLOCK + 1)
    return 4 * math.ceil(raw / 3)


MIN_TOKEN_LENGTH = token_length(0)


def looks_encrypted(value: str) -> bool:
    return (
        value.startswith(TOKEN_PREFIX)
        and len(value) >= MIN_TOKEN_LENGTH
        and len(value) % 4 == 0
    )


def _keys() -> tuple[str, ...]:
    return tuple(k for k in getattr(settings, "FIELD_ENCRYPTION_KEYS", ()) or () if k)


@lru_cache(maxsize=8)
def _fernet(keys: tuple[str, ...]) -> MultiFernet:
    """Keyed on the tuple, not memoised on the module, so override_settings in a
    test swaps the key without needing a cache-busting signal handler."""
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            f"MUNIM_FIELD_ENCRYPTION_KEYS contains something that is not a Fernet key "
            f"({exc}). Each entry is 44 urlsafe-base64 characters; generate one with "
            f"`python manage.py field_keygen`."
        ) from exc


class DecryptionFailed(Exception):
    """A stored token would not open with any configured key.

    Deliberately not swallowed. Returning the ciphertext would send it to Razorpay
    as a password and HMAC webhooks with it, and the operator would debug 401s for
    an afternoon instead of reading one sentence about the key.
    """


def encrypt(value: str) -> str:
    keys = _keys()
    if not keys:
        raise ImproperlyConfigured(
            "Cannot store an encrypted field: MUNIM_FIELD_ENCRYPTION_KEYS is empty. "
            "Run `python manage.py field_keygen` and put the printed line in .env."
        )
    return _fernet(keys).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not looks_encrypted(value):
        return value
    keys = _keys()
    if not keys:
        raise ImproperlyConfigured(
            "This row is encrypted and MUNIM_FIELD_ENCRYPTION_KEYS is empty. The key "
            "that wrote it is the only way to read it."
        )
    try:
        return _fernet(keys).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionFailed(
            "A stored secret did not open with any key in MUNIM_FIELD_ENCRYPTION_KEYS. "
            "Either a key was dropped from the list before its rows were re-encrypted, "
            "or the wrong .env is loaded."
        ) from exc


class MaxPlaintextBytes(BaseValidator):
    """max_length counts characters; the column has to hold UTF-8 bytes."""

    message = "Ensure this value is at most %(limit_value)d bytes (it is %(show_value)d)."
    code = "max_plaintext_bytes"

    def clean(self, x):
        return len(x.encode("utf-8"))

    def compare(self, a, b):
        return a > b


class EncryptedCharField(models.CharField):
    """CharField that is a Fernet token in Postgres and a str in Python.

    Declared with `max_plaintext_length` -- what a merchant may type -- because the
    ciphertext width is arithmetic, and hand-computed magic numbers in models.py rot
    the first time anyone touches the format.

    Not filterable. Fernet is randomised, so equality against a token is always
    false; get_lookup turns that silent wrong answer into a loud error.
    """

    def __init__(self, *args, max_plaintext_length: int, max_length: int | None = None,
                 **kwargs):
        self.max_plaintext_length = max_plaintext_length
        kwargs["max_length"] = max_length or token_length(max_plaintext_length)
        super().__init__(*args, **kwargs)
        self.validators.append(MaxPlaintextBytes(max_plaintext_length))

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["max_plaintext_length"] = self.max_plaintext_length
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        token = encrypt(value)
        if len(token) > self.max_length:
            raise ValueError(
                f"{self.model.__name__}.{self.name}: {len(value.encode())} bytes of "
                f"plaintext encrypts to {len(token)} characters, and the column is "
                f"varchar({self.max_length}). Widen max_plaintext_length; do not "
                f"truncate a secret."
            )
        return token

    def get_lookup(self, lookup_name):
        if lookup_name != "isnull":
            raise FieldError(
                f"{self.model.__name__}.{self.name} is encrypted with a randomised "
                f"cipher: `{lookup_name}` against it can only ever match nothing. "
                f"Filter on something else, or store a separate hash column."
            )
        return super().get_lookup(lookup_name)

from __future__ import annotations

import base64
import hmac
import hashlib
import struct
import time


def hotp(secret_base32: str, counter: int, *, digits: int = 6) -> str:
    secret = base64.b32decode(_normalize_base32(secret_base32), casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(secret, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def verify_totp(
    secret_base32: str,
    code: str,
    *,
    now: int | None = None,
    interval: int = 30,
    window: int = 1,
    digits: int = 6,
) -> bool:
    cleaned = "".join(ch for ch in code.strip() if ch.isdigit())
    if len(cleaned) != digits:
        return False
    current = int((time.time() if now is None else now) // interval)
    for offset in range(-window, window + 1):
        if hmac.compare_digest(hotp(secret_base32, current + offset, digits=digits), cleaned):
            return True
    return False


def _normalize_base32(value: str) -> str:
    cleaned = "".join(value.split()).replace("-", "").upper()
    padding = "=" * ((8 - len(cleaned) % 8) % 8)
    return cleaned + padding


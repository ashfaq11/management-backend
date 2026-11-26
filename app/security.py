import hashlib
import hmac
import os
import secrets

ITER = 200_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER)
    return f"pbkdf2${ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), dk)
    except ValueError:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

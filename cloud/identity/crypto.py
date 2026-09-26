"""Small standard-library/Fernet primitives, with no credential discovery."""
import hashlib
import hmac
import re
import secrets
from cryptography.fernet import Fernet


def new_token():
    return secrets.token_urlsafe(32)


def checked_ascii(raw, limit=4096):
    if not isinstance(raw, str) or not 1 <= len(raw) <= limit or not raw.isascii():
        raise ValueError('invalid_token')
    return raw.encode('ascii')


def hash_token(raw):
    return hashlib.sha256(checked_ascii(raw)).hexdigest()


def csrf_token(raw_cookie):
    return hmac.new(checked_ascii(raw_cookie), b'athena:csrf:v1', hashlib.sha256).hexdigest()


def valid_token(raw):
    return isinstance(raw, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', raw) is not None


def seal_verifier(key, value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', value):
        raise ValueError('invalid_verifier')
    return Fernet(key).encrypt(value.encode('ascii'))


def open_verifier(key, value):
    result = Fernet(key).decrypt(value).decode('ascii')
    if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', result):
        raise ValueError('invalid_verifier')
    return result

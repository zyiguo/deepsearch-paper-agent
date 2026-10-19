"""Versioned password hashing; session tokens are generated separately."""
import hashlib
import hmac
import secrets


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('密码长度须为 12–128 个字符')
    salt = secrets.token_hex(16)
    result = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                            n=32768, r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32)
    return f'scrypt-v1${salt}${result.hex()}'


def verify_password(password, stored):
    try:
        version, salt, expected = stored.split('$')
        if version != 'scrypt-v1' or len(salt) != 32 or len(expected) != 64 or len(password) > 128:
            return False
        actual = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                                n=32768, r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False

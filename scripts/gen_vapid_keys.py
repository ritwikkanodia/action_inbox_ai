"""Print a fresh VAPID key pair in .env form. Run once per deployment:

    python scripts/gen_vapid_keys.py >> .env

Rotating the keys invalidates every existing push subscription — users
re-enable notifications from Settings.
"""
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    vapid = Vapid()
    vapid.generate_keys()
    private = _b64url(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64url(vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    ))
    print(f"VAPID_PRIVATE_KEY={private}")
    print(f"VAPID_PUBLIC_KEY={public}")
    print("VAPID_SUBJECT=mailto:you@example.com")


if __name__ == "__main__":
    main()

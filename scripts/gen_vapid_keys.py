#!/usr/bin/env python3
"""Generate a VAPID keypair for web push — run once, locally.

VAPID is how a push service (Apple/Google/Mozilla) knows the notification came
from *you*. You need a matching pair:

  • the PUBLIC key  → goes in the Cloudflare Worker (VAPID_PUBLIC_KEY var) so the
    browser can subscribe. It is safe to expose.
  • the PRIVATE key → goes in GitHub as the VAPID_PRIVATE_KEY secret so the
    hourly Action can sign the pushes. Keep it secret.

Both are printed in the raw base64url form that both the browser's
PushManager.subscribe() and pywebpush (the sender) understand.

Usage:
    pip install cryptography      # already pulled in by pywebpush
    python scripts/gen_vapid_keys.py

(Equivalent if you have Node handy:  npx web-push generate-vapid-keys)
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())

    # Raw 32-byte private scalar — the form pywebpush/py_vapid load directly.
    priv_raw = key.private_numbers().private_value.to_bytes(32, "big")

    # Uncompressed public point (0x04 || X || Y), 65 bytes — the browser's
    # applicationServerKey.
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )

    print("=" * 70)
    print("VAPID keypair generated. Store these as described below.")
    print("=" * 70)
    print()
    print("PUBLIC KEY  → Cloudflare Worker var  VAPID_PUBLIC_KEY")
    print("            (also fine to expose in the browser):")
    print()
    print("    " + b64url(pub_raw))
    print()
    print("PRIVATE KEY → GitHub repo secret  VAPID_PRIVATE_KEY  (keep secret!):")
    print()
    print("    " + b64url(priv_raw))
    print()
    print("Also set a VAPID_SUBJECT secret to a contact URL, e.g.")
    print("    mailto:you@example.com")
    print("=" * 70)


if __name__ == "__main__":
    main()

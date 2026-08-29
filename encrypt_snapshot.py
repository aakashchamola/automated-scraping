"""
encrypt_snapshot.py — Encrypt the exported JSON for a public static site.

GitHub Pages is world-readable, so a password checked in JavaScript protects
nothing: the data sits next to the check, and anyone can fetch the JSON
directly. Instead the password *is* the key — each file is encrypted with
AES-256-GCM under a key derived from the passphrase, so without it the payload
is unreadable rather than merely hidden, and a wrong password fails as a
decryption error rather than a bypassable comparison.

Parameters are the ones the browser's built-in WebCrypto implements, so the
page needs no crypto library:

    PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte random salt
    AES-256-GCM, 12-byte random IV, 16-byte tag (appended by both sides)

All files in one publish share a salt, with a fresh IV each. That is what lets
the page derive the key once instead of once per worksheet, and — more usefully
— cache the derived CryptoKey as non-extractable so "stay signed in" never has
to keep the password itself. Sharing a salt is safe here because AES-GCM's
requirement is a unique IV per encryption under a key, not a unique key.

Usage:
    python encrypt_snapshot.py --in site/data --out site/data --password "$DASHBOARD_PASSWORD"
"""

import argparse
import base64
import glob
import hashlib
import json
import os
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
IV_BYTES = 12


def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               PBKDF2_ITERATIONS, dklen=32)


def encrypt_bytes(plaintext: bytes, password: str, salt: bytes = None) -> dict:
    salt = salt or os.urandom(SALT_BYTES)
    iv = os.urandom(IV_BYTES)
    ciphertext = AESGCM(derive_key(password, salt)).encrypt(iv, plaintext, None)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return {
        "v": 1,
        "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": PBKDF2_ITERATIONS,
                "salt": b64(salt)},
        "cipher": {"name": "AES-GCM", "iv": b64(iv)},
        "data": b64(ciphertext),
    }


def decrypt_payload(payload: dict, password: str) -> bytes:
    """Inverse of encrypt_bytes — used by the tests to prove the round trip."""
    salt = base64.b64decode(payload["kdf"]["salt"])
    iv = base64.b64decode(payload["cipher"]["iv"])
    key = derive_key(password, salt)
    return AESGCM(key).decrypt(iv, base64.b64decode(payload["data"]), None)


def main() -> None:
    ap = argparse.ArgumentParser(description="Encrypt exported JSON for a public site")
    ap.add_argument("--in", dest="src", default="site/data")
    ap.add_argument("--out", dest="dst", default="site/data")
    ap.add_argument("--password", default=os.environ.get("DASHBOARD_PASSWORD", ""))
    args = ap.parse_args()

    if not args.password:
        sys.exit("no password given (--password or DASHBOARD_PASSWORD)")
    if len(args.password) < 8:
        sys.exit("password must be at least 8 characters")

    os.makedirs(args.dst, exist_ok=True)
    salt = os.urandom(SALT_BYTES)      # one per publish; see the module docstring
    encrypted = 0
    for path in sorted(glob.glob(os.path.join(args.src, "*.json"))):
        name = os.path.basename(path)
        if name.endswith(".enc.json"):
            continue
        with open(path, "rb") as fh:
            plaintext = fh.read()
        payload = encrypt_bytes(plaintext, args.password, salt=salt)
        out_path = os.path.join(args.dst, name.replace(".json", ".enc.json"))
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        # The cleartext must never reach the published site.
        if os.path.abspath(path) != os.path.abspath(out_path):
            os.remove(path)
        print(f"encrypted {name} -> {os.path.basename(out_path)} "
              f"({os.path.getsize(out_path)/1024:.0f} KB)")
        encrypted += 1

    if not encrypted:
        sys.exit(f"nothing to encrypt in {args.src}")
    print(f"{encrypted} files encrypted; no cleartext left in {args.dst}")


if __name__ == "__main__":
    main()

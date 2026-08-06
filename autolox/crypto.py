"""Crypto helpers for the Loxone Miniserver protocol.

Everything here is stateless (functions) or a small session object. Derived
from pyloxone-api 0.2.4's api.py — see the [lib] provenance tags in
docs/protocol.md — plus the Loxone Communicating with the Miniserver docs.

Kept deliberately small: the enrolment tool only needs enough crypto to
complete the websocket handshake and reach the state stream. Command
encryption is included because the Miniserver requires it post-keyexchange,
but we never send secured NFC commands over the websocket (see project
memory — that dispatch path in pyloxone-api is broken; we use HTTP for
secured commands instead).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import urllib.parse
from dataclasses import dataclass, field

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad


def normalise_public_key(pk: str) -> str:
    """Loxone returns a cert-like blob with no newlines. Rewrite it into
    proper PEM so pycryptodome can parse it."""
    return pk.replace(
        "-----BEGIN CERTIFICATE-----", "-----BEGIN PUBLIC KEY-----\n"
    ).replace("-----END CERTIFICATE-----", "\n-----END PUBLIC KEY-----\n")


def rsa_encrypt_session_key(public_key_pem: str, aes_key: bytes, iv: bytes) -> str:
    """Build 'aes_key_hex:iv_hex', RSA-encrypt with the Miniserver's public
    key, return base64 (as the Miniserver expects on the wire)."""
    payload = f"{aes_key.hex()}:{iv.hex()}".encode()
    cipher = PKCS1_v1_5.new(RSA.importKey(public_key_pem))
    return base64.b64encode(cipher.encrypt(payload)).decode()


@dataclass
class Session:
    """AES session state. Key and IV are one per websocket connection.
    salt rolls forward and is included in every encrypted command."""

    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    iv: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    salt: str = field(default_factory=lambda: secrets.token_hex(8))

    def encrypt_command(self, command: str) -> str:
        """Wrap `command` for sending as `jdev/sys/enc/{blob}` on an
        encrypted websocket. Rolls salt forward each call.

        Key and IV stay fixed for the session - the Miniserver only learns
        them once, RSA-encrypted in the step-4 keyexchange, so it decrypts
        every subsequent command against that same pair. The salt is a
        plaintext freshness value inside the encrypted payload itself and
        has no such constraint, so it advances after every call."""
        plaintext = f"salt/{self.salt}/{command}\x00".encode("utf-8")
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        blob = base64.b64encode(cipher.encrypt(pad(plaintext, 16))).decode()
        self.salt = secrets.token_hex(8)
        return "jdev/sys/enc/" + urllib.parse.quote(blob)


def _hash(alg: str) -> "hashlib._Hash":
    if alg == "SHA256":
        return hashlib.sha256()
    if alg == "SHA1":
        return hashlib.sha1()
    raise ValueError(f"unsupported hash algorithm: {alg}")


def _hmac(alg: str, key: bytes, msg: bytes) -> str:
    algo = hashlib.sha256 if alg == "SHA256" else hashlib.sha1
    return hmac.new(key, msg, algo).hexdigest()


def password_hash(user: str, password: str, key_wire: str, salt_wire: str,
                  hash_alg: str) -> str:
    """Compute the login credential hash used for gettoken/getjwt.

    Formula (from Loxone API docs and pyloxone-api):
        inner = uppercase_hex(H(password + ':' + salt))
        outer = HMAC(bytes.fromhex(key), user + ':' + inner)

    key and salt come straight off the wire from getkey2 (they are already
    hex-encoded ASCII at that layer; they get passed to HMAC/hex-decode
    exactly as received)."""
    inner_h = _hash(hash_alg)
    inner_h.update(f"{password}:{salt_wire}".encode())
    inner = inner_h.hexdigest().upper()
    return _hmac(hash_alg, bytes.fromhex(key_wire), f"{user}:{inner}".encode())


def visu_hash(visu_password: str, key_wire: str, salt_wire: str,
              hash_alg: str) -> str:
    """Compute the visu-hash used to sign secured commands.

    Formula (from pyloxone-api._send_secured, proven working against a live
    Miniserver 2026-08-04):
        inner = uppercase_hex(H(visu_password + ':' + salt))
        outer = HMAC(bytes.fromhex(key), inner)

    Note: unlike password_hash the user is NOT prepended to the inner value.
    """
    inner_h = _hash(hash_alg)
    inner_h.update(f"{visu_password}:{salt_wire}".encode())
    inner = inner_h.hexdigest().upper()
    return _hmac(hash_alg, bytes.fromhex(key_wire), inner.encode())

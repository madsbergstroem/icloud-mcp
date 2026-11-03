"""Web Push to the owner's devices: a short note when something waits for approval or a waiting mail got its file.

A note says only what kind of item waits ("An email is waiting for your approval."), never who it goes to or what it
says. It is encrypted for the device (RFC 8291, aes128gcm) and signed for the push service with this server's own
VAPID key (RFC 8292), so the push service (Apple, Google, Mozilla) carries it without reading it.

Why not pywebpush: it pulls in requests and aiohttp, and its building block http-ece ships only as a source package,
which the egress-locked image build cannot fetch. The two steps it does are small and use only primitives the server
already has: ECDH, HKDF and AES-GCM from `cryptography`, an ES256 JWT from PyJWT. tests/test_push.py checks the
encryption against the worked example in RFC 8291 section 5.

DATA_DIR/push.json (mode 600) holds the VAPID key pair and the subscribed devices.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)
FILE = "push.json"
RECORD_SIZE = 4096
MAX_DEVICES = 10

# What a note says, per kind. Nothing from the item itself goes in.
TEXTS = {
    "mail": ("Approval needed", "An email is waiting for your approval."),
    "calendar": ("Approval needed", "A calendar change is waiting for your approval."),
    "imessage": ("Approval needed", "An iMessage is waiting for your approval."),
    "file": ("File arrived", "A waiting email got its file."),
    "test": ("Notifications are on", "This is how a note from your server looks."),
}


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes, *, as_private: ec.EllipticCurvePrivateKey | None = None,
            salt: bytes | None = None) -> bytes:
    """RFC 8291 message encryption with the aes128gcm content coding (RFC 8188), one record.

    ua_public: the device's p256dh key (65-byte uncompressed point); auth_secret: its 16-byte auth secret.
    as_private and salt are for the RFC's test vector only; otherwise both are fresh for every message."""
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _point(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("push payload too large")
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)          # \x02: the last (and only) record, no padding
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + body


class PushStore:
    def __init__(self, data_dir: str, subject: str):
        self.path = Path(data_dir) / FILE
        self.subject = subject              # VAPID "sub": how the push service can reach whoever runs this server
        self._lock = threading.Lock()
        self._devices: list[dict[str, Any]] = []
        self._key: ec.EllipticCurvePrivateKey | None = None
        self._load()

    # -- storage -------------------------------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        pem = data.get("vapid_private_key") if isinstance(data, dict) else None
        if pem:
            self._key = serialization.load_pem_private_key(pem.encode(), password=None)
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
        self._devices = [d for d in (data.get("devices") or []) if isinstance(d, dict) and d.get("endpoint")] if isinstance(data, dict) else []
        if not pem:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pem = self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".push-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"vapid_private_key": pem, "devices": self._devices}, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- devices -------------------------------------------------------------------------------------------------------
    @property
    def public_key(self) -> str:
        return b64u(_point(self._key.public_key()))

    @staticmethod
    def device_id(endpoint: str) -> str:
        return hashlib.sha256(endpoint.encode()).hexdigest()[:16]

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"id": self.device_id(d["endpoint"]), "label": d.get("label", ""), "created_at": d.get("created_at"),
                     "last_sent_at": d.get("last_sent_at"), "failing": bool(d.get("failures"))} for d in self._devices]

    def add(self, subscription: dict[str, Any], label: str) -> dict[str, Any]:
        endpoint = str(subscription.get("endpoint", ""))
        keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("This is not a push subscription.")
        try:
            p256dh, auth = unb64u(str(keys.get("p256dh", ""))), unb64u(str(keys.get("auth", "")))
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except (ValueError, TypeError):
            raise ValueError("The subscription's keys are not valid.") from None
        if len(auth) != 16:
            raise ValueError("The subscription's keys are not valid.")
        with self._lock:
            self._devices = [d for d in self._devices if d["endpoint"] != endpoint]
            if len(self._devices) >= MAX_DEVICES:
                raise ValueError(f"At most {MAX_DEVICES} devices get notifications. Remove one first.")
            self._devices.append({"endpoint": endpoint, "p256dh": b64u(p256dh), "auth": b64u(auth), "label": label[:60],
                                  "created_at": time.time(), "last_sent_at": None, "failures": 0})
            self._save()
        return {"id": self.device_id(endpoint)}

    def remove(self, device_id: str) -> bool:
        with self._lock:
            before = len(self._devices)
            self._devices = [d for d in self._devices if self.device_id(d["endpoint"]) != device_id]
            if len(self._devices) != before:
                self._save()
                return True
            return False

    # -- sending -------------------------------------------------------------------------------------------------------
    def _vapid(self, endpoint: str) -> str:
        parts = urlsplit(endpoint)
        token = jwt.encode({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": self.subject},
                           self._key, algorithm="ES256")
        return f"vapid t={token}, k={self.public_key}"

    def _send_one(self, device: dict[str, Any], payload: bytes) -> str:
        body = encrypt(payload, unb64u(device["p256dh"]), unb64u(device["auth"]))
        try:
            r = httpx.post(device["endpoint"], content=body, timeout=10, headers={
                "TTL": "86400", "Urgency": "high", "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                "Authorization": self._vapid(device["endpoint"])})
        except httpx.HTTPError as e:
            log.warning("Push to a device failed: %s", type(e).__name__)
            return "error"
        if r.status_code in (404, 410):
            return "gone"                       # the device unsubscribed or the browser dropped it
        if r.status_code >= 400:
            log.warning("Push service answered %s", r.status_code)
            return "error"
        return "ok"

    def send(self, kind: str, open_key: str = "", only: str = "") -> dict[str, int]:
        """Send the note for this kind to every device (or one). Returns counts; a device the push service no longer knows
        is removed."""
        title, text = TEXTS.get(kind, TEXTS["mail"])
        payload = json.dumps({"title": title, "body": text, "open": open_key}).encode()
        with self._lock:
            targets = [dict(d) for d in self._devices if not only or self.device_id(d["endpoint"]) == only]
        counts = {"ok": 0, "gone": 0, "error": 0}
        results = {}
        for d in targets:
            results[d["endpoint"]] = res = self._send_one(d, payload)
            counts[res] += 1
        with self._lock:
            kept = []
            for d in self._devices:
                res = results.get(d["endpoint"])
                if res == "gone":
                    continue
                if res == "ok":
                    d["last_sent_at"], d["failures"] = time.time(), 0
                elif res == "error":
                    d["failures"] = int(d.get("failures") or 0) + 1
                kept.append(d)
            self._devices = kept
            if results:
                self._save()
        return counts

    def notify(self, kind: str, open_key: str = "") -> None:
        """Fire and forget: a note never delays the tool call or the upload that caused it."""
        if not self._devices:
            return
        threading.Thread(target=self.send, args=(kind, open_key), name="icloud-push", daemon=True).start()

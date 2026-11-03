"""Web Push: the RFC 8291 encryption against the RFC's own example, a device's note decrypts on the device, and devices
the push service no longer knows are dropped."""
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from icloud_mcp import push
from icloud_mcp.push import b64u, unb64u

# RFC 8291 section 5 ("Push Message Encryption Example")
RFC = dict(
    plaintext=b"When I grow up, I want to be a watermelon",
    as_private="yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    ua_private="q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94",
    ua_public="BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    auth="BTBZMqHH6r4Tts7J_aSIgg",
    salt="DGv6ra1nlYgDCS1FRnbzlw",
    result="DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
           "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
)


def private(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(b64), "big"), ec.SECP256R1())


def decrypt(body: bytes, ua: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """What the browser does with a note (RFC 8291), written apart from push.encrypt."""
    salt, keylen = body[:16], body[20]
    as_public = body[21:21 + keylen]
    ua_public = ua.public_key().public_bytes(push.serialization.Encoding.X962, push.serialization.PublicFormat.UncompressedPoint)
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    hk = lambda s, k, i, n: HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=i).derive(k)  # noqa: E731
    ikm = hk(auth, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    plain = AESGCM(hk(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(hk(salt, ikm, b"Content-Encoding: nonce\x00", 12), body[21 + keylen:], None)
    return plain.rstrip(b"\x00")[:-1]


def test_the_encryption_matches_the_rfc_example():
    out = push.encrypt(RFC["plaintext"], unb64u(RFC["ua_public"]), unb64u(RFC["auth"]), as_private=private(RFC["as_private"]),
                       salt=unb64u(RFC["salt"]))
    assert b64u(out) == RFC["result"]
    assert decrypt(out, private(RFC["ua_private"]), unb64u(RFC["auth"])) == RFC["plaintext"]


class FakeService:
    def __init__(self, status=201):
        self.status, self.calls = status, []

    def __call__(self, url, content, timeout, headers):
        self.calls.append((url, content, headers))
        return httpx.Response(self.status)


def device():
    ua = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    pub = ua.public_key().public_bytes(push.serialization.Encoding.X962, push.serialization.PublicFormat.UncompressedPoint)
    return ua, auth, {"endpoint": "https://push.example.org/send/abc", "keys": {"p256dh": b64u(pub), "auth": b64u(auth)}}


def test_a_note_says_only_the_kind_and_decrypts_on_the_device(tmp_path, monkeypatch):
    store = push.PushStore(str(tmp_path), "https://approvals.example.ts.net")
    ua, auth, sub = device()
    store.add(sub, "iPhone · Safari")
    svc = FakeService()
    monkeypatch.setattr(push.httpx, "post", svc)
    assert store.send("mail", "mail:q1") == {"ok": 1, "gone": 0, "error": 0}
    url, body, headers = svc.calls[0]
    note = json.loads(decrypt(body, ua, auth))
    assert note == {"title": "Approval needed", "body": "An email is waiting for your approval.", "open": "mail:q1"}
    assert headers["Content-Encoding"] == "aes128gcm" and headers["Authorization"].startswith("vapid t=")
    token = headers["Authorization"].split("t=")[1].split(",")[0]
    claims = push.jwt.decode(token, store._key.public_key(), algorithms=["ES256"], audience="https://push.example.org")
    assert claims["sub"] == "https://approvals.example.ts.net"
    assert store.devices()[0]["last_sent_at"]


def test_a_device_the_push_service_no_longer_knows_is_dropped_and_keys_survive_a_restart(tmp_path, monkeypatch):
    store = push.PushStore(str(tmp_path), "mailto:owner@example.org")
    key = store.public_key
    _, _, sub = device()
    store.add(sub, "Mac")
    monkeypatch.setattr(push.httpx, "post", FakeService(410))
    assert store.send("test")["gone"] == 1 and store.devices() == []
    again = push.PushStore(str(tmp_path), "mailto:owner@example.org")
    assert again.public_key == key and oct((tmp_path / "push.json").stat().st_mode)[-3:] == "600"


@pytest.mark.parametrize("bad", [{"endpoint": "http://push.example.org/x", "keys": {}}, {"endpoint": "https://p.example.org/x",
                                  "keys": {"p256dh": "AAAA", "auth": "AAAA"}}])
def test_a_broken_subscription_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        push.PushStore(str(tmp_path), "mailto:owner@example.org").add(bad, "x")

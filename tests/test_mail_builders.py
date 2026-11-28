import base64
import email
from email import policy

import pytest

from icloud_mcp.mail import (
    MailError, build_forward, build_message, build_reply, extract_bodies, forward_subject, list_attachments,
    parse_addrs, recipient_allowed, reply_subject,
)

ME = ("Me Myself", "me@icloud.com")


def parse(raw: str | bytes):
    raw = raw.encode() if isinstance(raw, str) else raw
    return email.message_from_bytes(raw, policy=policy.default)


ORIG = parse(
    "From: Jane Doe <jane@example.org>\r\n"
    "To: Me Myself <me@icloud.com>, Bob <bob@example.org>\r\n"
    "Cc: Carol <carol@example.org>, me@icloud.com\r\n"
    "Subject: Lunch on Friday?\r\n"
    "Date: Sat, 19 Sep 2026 10:05:00 +0200\r\n"
    "Message-ID: <orig@example.org>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    "Hi,\r\nAre you free?\r\n\r\nJane\r\n"
)


def test_subject_prefixes():
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("Re: Hello") == "Re: Hello"
    assert reply_subject("RE: Hello") == "RE: Hello"
    assert reply_subject("Antw: Hallo") == "Antw: Hallo"
    assert reply_subject(None) == "Re:"
    assert forward_subject("Hello") == "Fwd: Hello"
    assert forward_subject("FW: Hello") == "FW: Hello"


def test_reply_threading_and_quote():
    r = build_reply(ORIG, sender=ME, body="Yes, noon works.")
    assert r["Subject"] == "Re: Lunch on Friday?"
    assert r["In-Reply-To"] == "<orig@example.org>"
    assert r["References"] == "<orig@example.org>"
    assert [a for _, a in parse_addrs(r["To"])] == ["jane@example.org"]
    assert r["Cc"] is None
    body = r.get_body(("plain",)).get_content()
    assert body.startswith("Yes, noon works.")
    assert "Jane Doe <jane@example.org> wrote:" in body
    assert "> Hi," in body and "> Are you free?" in body
    assert "\n>\n" in body  # blank quoted line


def test_reply_extends_reference_chain():
    o = parse(
        "From: a@x.org\r\nTo: me@icloud.com\r\nSubject: Re: t\r\nMessage-ID: <c@x>\r\n"
        "In-Reply-To: <b@x>\r\nReferences: <a@x> <b@x>\r\n\r\nbody\r\n"
    )
    r = build_reply(o, sender=ME, body="ok")
    assert r["References"] == "<a@x> <b@x> <c@x>"
    assert r["In-Reply-To"] == "<c@x>"
    assert r["Subject"] == "Re: t"


def test_reply_all_excludes_me_and_dedupes():
    r = build_reply(ORIG, sender=ME, body="ok", reply_all=True)
    to = [a for _, a in parse_addrs(r["To"])]
    cc = [a for _, a in parse_addrs(r["Cc"])]
    assert to == ["jane@example.org", "bob@example.org"]
    assert cc == ["carol@example.org"]
    assert "me@icloud.com" not in to + cc


def test_reply_honours_reply_to_and_override():
    o = parse("From: a@x.org\r\nReply-To: list@x.org\r\nTo: me@icloud.com\r\nSubject: s\r\nMessage-ID: <1@x>\r\n\r\nhi\r\n")
    assert [a for _, a in parse_addrs(build_reply(o, sender=ME, body="x")["To"])] == ["list@x.org"]
    r = build_reply(o, sender=ME, body="x", to=[("", "other@y.org")])
    assert [a for _, a in parse_addrs(r["To"])] == ["other@y.org"]


def test_reply_to_own_sent_message_goes_to_original_recipients():
    o = parse("From: me@icloud.com\r\nTo: dave@x.org\r\nSubject: s\r\nMessage-ID: <1@x>\r\n\r\nhi\r\n")
    assert [a for _, a in parse_addrs(build_reply(o, sender=ME, body="ping")["To"])] == ["dave@x.org"]


def test_reply_html_is_multipart_alternative_with_blockquote():
    r = build_reply(ORIG, sender=ME, body="plain", body_html="<p>rich</p>")
    assert r.get_content_type() == "multipart/alternative"
    html = r.get_body(("html",)).get_content()
    assert "<p>rich</p>" in html and "<blockquote" in html and "Are you free?" in html


def test_reply_without_quote():
    r = build_reply(ORIG, sender=ME, body="short", quote=False)
    assert "wrote:" not in r.get_body(("plain",)).get_content()


def test_signature_precedes_quote():
    r = build_reply(ORIG, sender=ME, body="hi", signature="Alex")
    text = r.get_body(("plain",)).get_content()
    assert text.index("-- \nAlex") < text.index("wrote:")


def test_forward_block_and_attachments():
    att = base64.b64encode(b"%PDF-1.4 fake").decode()
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"], m["Date"] = "Jane <jane@x.org>", "me@icloud.com", "Report", "<r@x>", "Sat, 19 Sep 2026 10:05:00 +0200"
    m.set_content("see attached")
    m.add_attachment(base64.b64decode(att), maintype="application", subtype="pdf", filename="q3.pdf")
    original = parse(m.as_bytes())
    f = build_forward(original, sender=ME, to=[("", "boss@x.org")], note="FYI")
    assert f["Subject"] == "Fwd: Report"
    assert f["In-Reply-To"] is None
    text = f.get_body(("plain",)).get_content()
    assert text.startswith("FYI") and "---------- Forwarded message" in text and "From: Jane <jane@x.org>" in text and "see attached" in text
    atts = list_attachments(f)
    assert [a["filename"] for a in atts] == ["q3.pdf"]
    assert f.get_content_type() == "multipart/mixed"


def test_build_message_attachments_unicode_and_bcc():
    att = {"filename": "hello.txt", "content_base64": base64.b64encode("héllo".encode()).decode()}
    m = build_message(
        sender=("Alex Álì", "me@icloud.com"), to=[("", "a@x.org")], bcc=[("", "hidden@x.org")], subject="Grüße – test",
        text="Café ☕", attachments=[att],
    )
    parsed = parse(m.as_bytes())
    assert parsed["Subject"] == "Grüße – test"
    assert parsed["Bcc"] == "hidden@x.org"  # kept for the Sent copy; smtplib strips it on the wire
    assert extract_bodies(parsed)[0].strip() == "Café ☕"
    assert list_attachments(parsed)[0]["filename"] == "hello.txt"
    assert parsed["Message-ID"].endswith("@icloud.com>")


EML = b"To: Support <help@example.org>\r\nSubject: Login codes\r\nX-Unsent: 1\r\n\r\nHi team,\r\nthe code never arrives.\r\n"


def test_an_eml_attachment_goes_out_as_the_mail_it_is():
    """An .eml (message/rfc822) is attached unencoded, as RFC 2046 asks, and reads back as the same mail. Before, the part was
    written base64 and attachment_bytes raised AttributeError on it."""
    from icloud_mcp.mail import attachment_bytes
    m = build_message(sender=ME, to=[("", "a@x.org")], subject="s", text="see attached",
                      attachments=[{"filename": "Support.eml", "content_type": "message/rfc822",
                                    "content_base64": base64.b64encode(EML).decode()}])
    part = next(m.iter_attachments())
    assert part.get_content_type() == "message/rfc822" and part["Content-Transfer-Encoding"] != "base64"
    assert parse(attachment_bytes(part))["X-Unsent"] == "1"
    got = next(parse(m.as_bytes(policy=policy.SMTP)).iter_attachments())
    assert got.get_filename() == "Support.eml"
    inner = parse(attachment_bytes(got))
    assert inner["Subject"] == "Login codes" and "the code never arrives." in inner.get_content()
    assert [a["filename"] for a in list_attachments(parse(m.as_bytes()))] == ["Support.eml"]


def test_another_message_type_goes_out_as_a_file():
    m = build_message(sender=ME, to=[("", "a@x.org")], subject="s", text="x",
                      attachments=[{"filename": "part.bin", "content_type": "message/partial", "content_base64": base64.b64encode(b"abc").decode()}])
    part = next(m.iter_attachments())
    assert part.get_content_type() == "application/octet-stream" and part.get_content() == b"abc"


def test_a_base64_eml_from_another_program_reads_as_its_bytes():
    """Another program may encode an attached mail base64, which RFC 2046 forbids; attachment_bytes gives its real bytes."""
    from icloud_mcp.mail import attachment_bytes
    raw = (b"From: a@x.org\r\nTo: me@icloud.com\r\nSubject: s\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
           b"--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n--b\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n"
           b"Content-Disposition: attachment; filename=x.eml\r\n\r\n" + base64.b64encode(EML) + b"\r\n--b--\r\n")
    assert attachment_bytes(next(parse(raw).iter_attachments())) == EML


def test_html_only_original_is_quoted_as_text():
    o = parse("From: a@x.org\r\nTo: me@icloud.com\r\nSubject: s\r\nMessage-ID: <1@x>\r\nContent-Type: text/html\r\n\r\n<html><body><p>Hello <b>there</b></p></body></html>\r\n")
    r = build_reply(o, sender=ME, body="ok")
    assert "> Hello **there**" in r.get_body(("plain",)).get_content()


def test_header_injection_rejected():
    with pytest.raises(MailError):
        build_message(sender=ME, to=[("", "a@x.org")], subject="hi\r\nBcc: evil@x.org", text="x")


def test_recipient_allowlist():
    assert recipient_allowed("anyone@x.org", ())
    assert recipient_allowed("a@x.org", ("a@x.org",))
    assert recipient_allowed("b@x.org", ("@x.org",))
    assert recipient_allowed("b@x.org", ("x.org",))
    assert not recipient_allowed("b@y.org", ("x.org", "a@y.org"))

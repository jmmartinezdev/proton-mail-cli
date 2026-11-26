from dataclasses import replace
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path

import pytest

from proton_mail_cli.errors import InputValidationError
from proton_mail_cli.message_builder import build_message
from proton_mail_cli.models import MessageRequest


@pytest.fixture
def mail_request(tmp_path):
    body = tmp_path / "body.txt"
    body.write_bytes("Café, 世界\nSecond line\n".encode())
    return MessageRequest(
        sender="Billing+tag@Example.com",
        recipient="customer@example.net",
        subject="Invoice — café",
        body=body,
        allowed_senders=("billing+tag@example.com",),
    )


def _round_trip(mail_request):
    message = build_message(mail_request)
    assert message.policy is SMTP
    return BytesParser(policy=SMTP).parsebytes(message.as_bytes())


def test_plain_message_round_trip(mail_request, monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        pytest.fail("Message construction must not resolve the local hostname")

    monkeypatch.setattr("socket.getfqdn", unexpected_lookup)
    message = _round_trip(mail_request)
    assert message["From"].addresses[0].addr_spec == mail_request.sender
    assert message["To"].addresses[0].addr_spec == mail_request.recipient
    assert message["Subject"] == mail_request.subject
    assert message["Date"].datetime is not None
    assert message["Message-ID"].endswith("@Example.com>")
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == "Café, 世界\r\nSecond line\r\n"
    assert list(message.iter_attachments()) == []


@pytest.mark.parametrize("with_alternative", [False, True])
def test_html_message_round_trip(mail_request, tmp_path, with_alternative):
    html = tmp_path / "body.HTML"
    html.write_text("<p>Café, 世界</p>", encoding="utf-8")
    plain = mail_request.body if with_alternative else None
    message = _round_trip(replace(mail_request, body=html, plain_body=plain))
    if with_alternative:
        assert message.get_content_type() == "multipart/alternative"
        text, html_part = message.iter_parts()
        assert text.get_content_type() == "text/plain"
        assert text.get_content() == "Café, 世界\r\nSecond line\r\n"
    else:
        html_part = message
    assert html_part.get_content_type() == "text/html"
    assert html_part.get_content() == "<p>Café, 世界</p>\r\n"


def test_attachments_round_trip_in_request_order(mail_request, tmp_path):
    nested = tmp_path / "private"
    nested.mkdir()
    first = nested / "café.pdf"
    second = tmp_path / "another.pdf"
    third = tmp_path / "binary.unknown-test-extension"
    payloads = (b"%PDF-first\x00\xff", b"%PDF-second\r\n", bytes(range(256)))
    for path, data in zip((first, second, third), payloads, strict=True):
        path.write_bytes(data)

    html = tmp_path / "body.html"
    html.write_text("<p>Hello</p>", encoding="utf-8")
    message = _round_trip(
        replace(
            mail_request,
            body=html,
            plain_body=mail_request.body,
            attachments=(first, second, third),
        )
    )
    assert message.get_content_type() == "multipart/mixed"
    assert next(message.iter_parts()).get_content_type() == "multipart/alternative"
    parts = list(message.iter_attachments())
    assert [part.get_filename() for part in parts] == [
        first.name,
        second.name,
        third.name,
    ]
    assert [part.get_content_type() for part in parts] == [
        "application/pdf",
        "application/pdf",
        "application/octet-stream",
    ]
    assert [part.get_payload(decode=True) for part in parts] == list(payloads)


def test_attached_email_preserves_raw_bytes(mail_request, tmp_path):
    path = tmp_path / "original.eml"
    original = b"Subject: Original\r\n\r\nBody without a trailing newline"
    path.write_bytes(original)
    (part,) = _round_trip(replace(mail_request, attachments=(path,))).iter_attachments()
    assert part.get_filename() == "original.eml"
    assert part.get_content_type() == "application/octet-stream"
    assert part.get_payload(decode=True) == original


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("sender", "Name <billing+tag@example.com>", "From.*ASCII mailbox"),
        ("sender", "billing@example.com, other@example.com", "From.*ASCII mailbox"),
        ("sender", "billing@example.com\r\nBcc: other@example.com", "From.*mailbox"),
        ("recipient", "a@example.com, b@example.com", "To.*ASCII mailbox"),
        ("recipient", "a@example.com\n", "To.*ASCII mailbox"),
        ("recipient", "a..b@example.com", "To.*ASCII mailbox"),
        ("recipient", "a@-example.com", "To.*ASCII mailbox"),
        ("recipient", "é@example.com", "To.*ASCII mailbox"),
        ("recipient", "=?UTF-8?Q?other?=@example.com", "To.*ASCII mailbox"),
        ("subject", "private subject\r\nBcc: other@example.com", "Subject.*line"),
        ("sender", "not-allowed@example.com", "not a configured sender"),
        ("max_message_bytes", 0, "positive byte count"),
    ],
)
def test_invalid_fields(mail_request, field, value, error):
    with pytest.raises(InputValidationError, match=error) as exc:
        build_message(replace(mail_request, **{field: value}))
    assert str(value) not in str(exc.value)


def test_missing_and_non_regular_inputs(mail_request, tmp_path):
    missing = tmp_path / "missing.txt"
    directory = tmp_path / "folder.txt"
    directory.mkdir()
    for path, error in ((missing, "does not exist"), (directory, "regular file")):
        with pytest.raises(InputValidationError, match=f"Body.*{error}"):
            build_message(replace(mail_request, body=path))
        with pytest.raises(InputValidationError, match=f"Attachment 1.*{error}"):
            build_message(replace(mail_request, attachments=(path,)))


def test_body_extensions(mail_request, tmp_path):
    with pytest.raises(InputValidationError, match=r"Body.*\.txt or \.html"):
        build_message(replace(mail_request, body=tmp_path / "body.md"))
    with pytest.raises(InputValidationError, match="requires an HTML body"):
        build_message(replace(mail_request, plain_body=mail_request.body))
    with pytest.raises(InputValidationError, match=r"alternative.*\.txt"):
        build_message(
            replace(
                mail_request,
                body=tmp_path / "body.html",
                plain_body=tmp_path / "plain.md",
            )
        )


def test_invalid_utf8_bodies(mail_request, tmp_path):
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    with pytest.raises(InputValidationError, match="Body.*valid UTF-8"):
        build_message(replace(mail_request, body=invalid))

    html = tmp_path / "body.html"
    html.write_text("<p>Hello</p>", encoding="utf-8")
    with pytest.raises(InputValidationError, match="alternative.*valid UTF-8"):
        build_message(replace(mail_request, body=html, plain_body=invalid))


@pytest.mark.parametrize("is_attachment", [False, True])
def test_unreadable_file(mail_request, tmp_path, monkeypatch, is_attachment):
    path = tmp_path / "denied.txt"
    path.touch()
    real_open = Path.open

    def limited_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError("private filesystem details")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", limited_open)
    fields = {"attachments": (path,)} if is_attachment else {"body": path}
    role = "attachment 1" if is_attachment else "body"
    with pytest.raises(InputValidationError, match=f"Could not read the {role}") as exc:
        build_message(replace(mail_request, **fields))
    assert "private filesystem details" not in str(exc.value)


@pytest.mark.parametrize("attachment_sizes", [(58, 20), (0, 57)])
def test_size_limit_counts_complete_wire_message_before_reading_attachments(
    mail_request, tmp_path, monkeypatch, attachment_sizes
):
    monkeypatch.setattr(
        "proton_mail_cli.message_builder.make_msgid",
        lambda *, domain: f"<fixed@{domain}>",
    )
    paths = (tmp_path / "café résumé.pdf", tmp_path / "data.bin")
    for path, size in zip(paths, attachment_sizes, strict=True):
        path.write_bytes(b"\xff" * size)
    mail_request = replace(mail_request, attachments=paths)
    wire_size = len(build_message(mail_request).as_bytes())
    assert (
        len(
            build_message(replace(mail_request, max_message_bytes=wire_size)).as_bytes()
        )
        == wire_size
    )

    real_open = Path.open

    def no_attachment_reads(self, *args, **kwargs):
        if self in paths:
            pytest.fail("Oversized message opened an attachment")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_attachment_reads)
    with pytest.raises(InputValidationError, match="size limit"):
        build_message(replace(mail_request, max_message_bytes=wire_size - 1))
    with pytest.raises(InputValidationError, match="size limit"):
        build_message(replace(mail_request, max_message_bytes=1))

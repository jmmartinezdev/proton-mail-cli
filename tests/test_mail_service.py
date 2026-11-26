from dataclasses import replace
from email.policy import SMTP
from unittest.mock import Mock, call

import pytest

from proton_mail_cli import mail_service
from proton_mail_cli.errors import InputValidationError, UncertainOutcomeError
from proton_mail_cli.models import (
    BridgeConfig,
    DryRunResult,
    MessageRequest,
    OperationResult,
)


@pytest.fixture
def inputs(tmp_path):
    config = BridgeConfig(
        host="localhost",
        smtp_port=1025,
        imap_port=1143,
        username="bridge-user",
        security="starttls",
        timeout=30,
        certificate=tmp_path / "bridge.pem",
        allowed_senders=("sender@example.com",),
    )
    body = tmp_path / "body.txt"
    body.write_text("Example content", encoding="utf-8")
    attachment = tmp_path / "attachment.bin"
    attachment.write_bytes(b"\x00\xff\x01")
    request = MessageRequest(
        sender=config.allowed_senders[0],
        recipient="recipient@example.net",
        subject="Example",
        body=body,
        allowed_senders=config.allowed_senders,
        attachments=(attachment,),
    )
    return request, config


@pytest.fixture
def calls(monkeypatch):
    steps = Mock()
    steps.attach_mock(Mock(wraps=mail_service.build_message), "build")
    steps.password.return_value = "test-password"
    steps.smtp.return_value = OperationResult("send", "SMTP accepted.")
    steps.imap.return_value = OperationResult("draft", "IMAP accepted.")
    monkeypatch.setattr(mail_service, "build_message", steps.build)
    monkeypatch.setattr(mail_service, "send_message", steps.smtp)
    monkeypatch.setattr(mail_service, "save_draft", steps.imap)
    return steps


@pytest.mark.parametrize(
    ("operation", "adapter"), [("send", "smtp"), ("draft", "imap")]
)
def test_real_operation_builds_then_gets_password_then_submits(
    operation, adapter, calls, inputs
):
    request, config = inputs
    result = mail_service.process_message(
        operation, request, config, dry_run=False, password_provider=calls.password
    )
    chosen = getattr(calls, adapter)
    message, _, _ = chosen.call_args.args
    (attachment,) = message.iter_attachments()
    assert attachment.get_payload(decode=True) == b"\x00\xff\x01"
    assert result == chosen.return_value
    assert calls.mock_calls == [
        call.build(request),
        call.password(config.username),
        getattr(call, adapter)(message, config, "test-password"),
    ]


@pytest.mark.parametrize("operation", ["send", "draft"])
def test_dry_run_reports_sizes_from_built_message(operation, calls, inputs):
    request, config = inputs
    message = mail_service.build_message(request)
    calls.reset_mock()
    calls.build.return_value = message

    result = mail_service.process_message(
        operation, request, config, dry_run=True, password_provider=calls.password
    )

    assert result == DryRunResult(operation, len(message.as_bytes(policy=SMTP)), (3,))
    assert calls.mock_calls == [call.build(request)]


def test_invalid_file_stops_before_credentials(calls, inputs, tmp_path):
    request, config = inputs
    invalid = replace(request, body=tmp_path / "missing.txt")
    with pytest.raises(InputValidationError, match="does not exist"):
        mail_service.process_message(
            "send", invalid, config, dry_run=False, password_provider=calls.password
        )
    assert calls.mock_calls == [call.build(invalid)]


def test_uncertain_draft_is_not_retried_or_sent(calls, inputs):
    request, config = inputs
    error = UncertainOutcomeError("Check web Drafts before retrying.")
    calls.imap.side_effect = error
    with pytest.raises(UncertainOutcomeError) as exc:
        mail_service.process_message(
            "draft", request, config, dry_run=False, password_provider=calls.password
        )
    assert exc.value is error
    calls.imap.assert_called_once()
    calls.smtp.assert_not_called()

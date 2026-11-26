import warnings
from unittest.mock import Mock

import pytest

from proton_mail_cli import cli, mail_service
from proton_mail_cli.config import load_config, staged_config
from proton_mail_cli.errors import (
    BridgeAuthenticationError,
    BridgeConnectionError,
    BridgeRejectionError,
    BridgeTLSError,
    CredentialError,
    UncertainOutcomeError,
)
from proton_mail_cli.models import BridgeConfig, OperationResult


@pytest.fixture
def prompts(monkeypatch, certificate):
    def set_answers(*, host="", overwrite=()):
        path, _ = certificate
        answers = iter(
            [
                *overwrite,
                host,
                "",
                "",
                "bridge-user",
                "",
                "",
                str(path),
                "",
                "a@example.com",
            ]
        )
        monkeypatch.setattr("builtins.input", lambda _: next(answers))

    password = Mock(return_value="test-password")
    monkeypatch.setattr(cli.getpass, "getpass", password)
    set_answers()
    return set_answers, password


@pytest.mark.parametrize("override", ["before", "after", "xdg"])
def test_configure_saves_settings_and_password(
    override, prompts, secure_backend, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "custom.toml"
    if override == "before":
        args = ["--config", str(path), "configure"]
    elif override == "after":
        args = ["configure", "--config", str(path)]
    else:
        path = tmp_path / "xdg/proton-mail-cli/config.toml"
        args = ["configure"]
    assert cli.main(args) == 0
    secure_backend.set_password.assert_called_once_with(
        "proton-mail-cli", "bridge-user", "test-password"
    )
    assert load_config(path).allowed_senders == ("a@example.com",)
    assert "test-password" not in path.read_text(encoding="utf-8")
    output = capsys.readouterr()
    assert "No mail was sent" in output.out
    assert output.err == ""
    assert "test-password" not in output.out


def test_invalid_settings_do_not_prompt_for_password(
    prompts, secure_backend, tmp_path, capsys
):
    set_answers, password = prompts
    set_answers(host="192.168.0.1")
    path = tmp_path / "config.toml"
    assert cli.main(["configure", "--config", str(path)]) == 3
    assert "loopback" in capsys.readouterr().err
    assert not path.exists()
    password.assert_not_called()
    secure_backend.set_password.assert_not_called()


def test_keyring_failure_preserves_existing_file(
    prompts, secure_backend, tmp_path, capsys
):
    set_answers, _ = prompts
    set_answers(overwrite=("y",))
    path = tmp_path / "config.toml"
    path.write_text("original", encoding="utf-8")
    secure_backend.set_password.side_effect = RuntimeError("test-password")
    assert cli.main(["configure", "--config", str(path)]) == 3
    output = capsys.readouterr()
    assert "Could not store" in output.err
    assert "test-password" not in output.err
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".config-*")) == []


def test_getpass_echo_fallback_is_rejected(prompts, secure_backend, tmp_path, capsys):
    _, password = prompts

    def warn(_):
        warnings.warn("password may echo", cli.getpass.GetPassWarning, stacklevel=1)

    password.side_effect = warn
    path = tmp_path / "config.toml"
    assert cli.main(["configure", "--config", str(path)]) == 3
    assert "interactive terminal" in capsys.readouterr().err
    assert not path.exists()
    secure_backend.set_password.assert_not_called()


def test_help_advertises_all_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "{configure,send,draft}" in capsys.readouterr().out


@pytest.fixture
def mail_args(tmp_path):
    body = tmp_path / "private-body.txt"
    body.write_text("Private body content.", encoding="utf-8")
    return [
        "--from",
        "a@example.com",
        "--to",
        "private-recipient@example.net",
        "--subject",
        "Private subject",
        "--body",
        str(body),
    ]


@pytest.fixture
def mail_settings(tmp_path, monkeypatch, certificate):
    certificate_path, _ = certificate
    config = BridgeConfig(
        host="localhost",
        smtp_port=1025,
        imap_port=1143,
        username="bridge-user",
        security="starttls",
        timeout=30,
        certificate=certificate_path,
        allowed_senders=("a@example.com",),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "unused-default"))
    path = tmp_path / "custom.toml"
    with staged_config(path, config):
        pass
    return path, config


@pytest.fixture
def mail_calls(monkeypatch):
    password = Mock(return_value="private Bridge password")
    send = Mock(return_value=OperationResult("send", "Bridge accepted the message."))
    draft = Mock(return_value=OperationResult("draft", "Bridge accepted the draft."))
    prompt = Mock(side_effect=AssertionError("Mail commands must not prompt"))
    monkeypatch.setattr(cli, "get_password", password)
    monkeypatch.setattr(mail_service, "send_message", send)
    monkeypatch.setattr(mail_service, "save_draft", draft)
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    return password, send, draft, prompt


@pytest.mark.parametrize("command", ["send", "draft"])
@pytest.mark.parametrize("flag", ["--from", "--to", "--subject", "--body"])
def test_mail_required_flags(command, flag, mail_args, mail_calls, capsys):
    index = mail_args.index(flag)
    del mail_args[index : index + 2]
    with pytest.raises(SystemExit) as exc:
        cli.main([command, *mail_args])
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert flag in output.err  # Usage shows the required flags without input data.
    for mock in mail_calls:
        mock.assert_not_called()


def test_parser_errors_do_not_echo_unrecognized_values(mail_args, mail_calls, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["send", *mail_args, "--dry-run", "private unexpected value"])
    assert exc.value.code == 2
    assert "private unexpected value" not in capsys.readouterr().err
    for mock in mail_calls:
        mock.assert_not_called()


@pytest.mark.parametrize(
    ("command", "override"), [("send", "before"), ("draft", "after")]
)
def test_mail_dispatch_and_config_override(
    command, override, mail_args, mail_settings, mail_calls, tmp_path, capsys
):
    path, config = mail_settings
    password, send, draft, prompt = mail_calls
    plain = mail_args[-1]
    html = tmp_path / "body.html"
    html.write_text("<p>Example</p>", encoding="utf-8")
    mail_args[-1] = str(html)
    files = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for file in files:
        file.write_bytes(b"PDF data")
    options = [
        *mail_args,
        "--plain-body",
        plain,
        "--attach",
        str(files[0]),
        "--attach",
        str(files[1]),
    ]
    config_args = ["--config", str(path)]
    args = (
        [*config_args, command, *options]
        if override == "before"
        else [command, *options, *config_args]
    )

    assert cli.main(args) == 0

    password.assert_called_once_with(config.username)
    chosen, other = (send, draft) if command == "send" else (draft, send)
    chosen.assert_called_once()
    other.assert_not_called()
    prompt.assert_not_called()
    message, actual_config, actual_password = chosen.call_args.args
    assert actual_config == config
    assert actual_password == password.return_value
    assert [part.get_filename() for part in message.iter_attachments()] == [
        file.name for file in files
    ]
    alternative = next(message.iter_parts())
    assert [part.get_content_type() for part in alternative.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    output = capsys.readouterr()
    assert output.out == chosen.return_value.summary + "\n"
    assert not output.err


@pytest.mark.parametrize("command", ["send", "draft"])
def test_dry_run_outputs_only_sizes(
    command, mail_args, mail_settings, mail_calls, tmp_path, capsys
):
    path, _ = mail_settings
    attachment = tmp_path / "private-attachment.txt"
    attachment.write_bytes(b"Private attachment content.")
    second = tmp_path / "other.txt"
    second.write_bytes(b"abc")
    args = [
        command,
        *mail_args,
        "--config",
        str(path),
        "--dry-run",
        "--attach",
        str(attachment),
        "--attach",
        str(second),
    ]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert not output.err
    assert f"Dry run ({command}):" in output.out
    assert "bytes serialized" in output.out
    assert "2 attachment(s) (27 bytes, 3 bytes)" in output.out
    assert "private" not in output.out.lower()
    for mock in mail_calls:
        mock.assert_not_called()


def test_dry_run_requires_valid_config(mail_args, mail_calls, capsys):
    # Pass a file that cannot be a valid TOML configuration.
    assert cli.main(["send", *mail_args, "--dry-run", "--config", mail_args[-1]]) == 3
    output = capsys.readouterr()
    assert not output.out
    assert "TOML" in output.err
    for mock in mail_calls:
        mock.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_sender_fails_before_credential_lookup(
    dry_run, mail_args, mail_settings, mail_calls, capsys
):
    path, _ = mail_settings
    mail_args[1] = "unlisted@example.com"
    options = ["--dry-run"] if dry_run else []
    assert cli.main(["send", *mail_args, "--config", str(path), *options]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "not a configured sender" in output.err
    for mock in mail_calls:
        mock.assert_not_called()


@pytest.mark.parametrize("command", ["send", "draft"])
def test_missing_attachment_never_reaches_credentials_or_transport(
    command, mail_args, mail_settings, mail_calls, tmp_path, capsys
):
    path, _ = mail_settings
    missing = tmp_path / "private-missing.pdf"
    args = [
        command,
        *mail_args,
        "--config",
        str(path),
        "--attach",
        str(missing),
    ]
    assert cli.main(args) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "Attachment 1 file does not exist" in output.err
    assert missing.name not in output.err
    for mock in mail_calls:
        mock.assert_not_called()


@pytest.mark.parametrize(
    ("error", "stage", "code", "diagnostic"),
    [
        (
            CredentialError("Bridge password not found."),
            "lookup",
            3,
            "password not found",
        ),
        (
            BridgeConnectionError("Bridge unavailable."),
            "draft",
            4,
            "Bridge unavailable",
        ),
        (BridgeTLSError("TLS verification failed."), "draft", 4, "TLS verification"),
        (BridgeAuthenticationError("IMAP login failed."), "draft", 5, "login failed"),
        (BridgeRejectionError("No Drafts mailbox."), "draft", 6, "No Drafts mailbox"),
        (
            UncertainOutcomeError("Check Proton Mail web Drafts before retrying."),
            "draft",
            7,
            "Check Proton Mail web Drafts before retrying",
        ),
        (
            RuntimeError("private internal details"),
            "draft",
            1,
            "Unexpected internal failure",
        ),
    ],
)
def test_exit_code_mapping_and_no_draft_fallback(
    error, stage, code, diagnostic, mail_args, mail_settings, mail_calls, capsys
):
    path, _ = mail_settings
    password, send, draft, _ = mail_calls
    if stage == "lookup":
        password.side_effect = error
    else:
        draft.side_effect = error
    assert cli.main(["draft", *mail_args, "--config", str(path)]) == code
    output = capsys.readouterr()
    assert not output.out
    assert diagnostic in output.err
    assert "Traceback" not in output.err
    assert "private" not in output.err
    send.assert_not_called()
    if stage == "lookup":
        draft.assert_not_called()
    else:
        draft.assert_called_once()

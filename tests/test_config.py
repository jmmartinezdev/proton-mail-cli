import os
import ssl
import stat
from pathlib import Path

import pytest

from proton_mail_cli.config import (
    config_path,
    load_config,
    staged_config,
    validate_config,
)
from proton_mail_cli.errors import ConfigurationError


@pytest.fixture
def settings(certificate):
    path, _ = certificate
    return {
        "bridge": {
            "host": "localhost",
            "smtp_port": 1025,
            "imap_port": 1143,
            "username": 'bridge"user',
            "security": "starttls",
            "timeout": 30,
            "certificate": str(path),
            "drafts_mailbox": 'Special "Drafts"',
        },
        "mail": {"allowed_senders": ["sales@example.com", "name+tag@example.com"]},
    }


def test_default_and_override_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    default = tmp_path / ".config/proton-mail-cli/config.toml"
    assert config_path() == default
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert config_path() == default
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == tmp_path / "xdg/proton-mail-cli/config.toml"
    assert config_path(Path("custom.toml")) == Path("custom.toml")


def test_round_trip(settings, certificate, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config_path()
    config = validate_config(settings)
    with staged_config(path, config):
        assert not path.exists()
    assert load_config() == config
    assert load_config(path) == config
    certificate_path, context = certificate
    context.load_verify_locations.assert_called_with(cafile=certificate_path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_load_missing_or_invalid_config(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(path)
    for malformed in (b"[bridge", b"\xff"):
        path.write_bytes(malformed)
        with pytest.raises(ConfigurationError, match="valid UTF-8 TOML"):
            load_config(path)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.2.3.4", "::1", "localhost"])
def test_loopback_hosts(settings, host):
    settings["bridge"]["host"] = host
    assert validate_config(settings).host == host


@pytest.mark.parametrize(
    ("table", "field", "value", "error"),
    [
        ("bridge", "host", "example.com", "loopback"),
        ("bridge", "host", "192.168.1.2", "loopback"),
        ("bridge", "host", "::", "loopback"),
        ("bridge", "smtp_port", 0, "smtp_port"),
        ("bridge", "imap_port", 65536, "imap_port"),
        ("bridge", "smtp_port", True, "smtp_port"),
        ("bridge", "imap_port", "1143", "imap_port"),
        ("bridge", "username", "", "username"),
        ("bridge", "timeout", float("nan"), "timeout"),
        ("bridge", "timeout", float("inf"), "timeout"),
        ("bridge", "timeout", -1, "timeout"),
        ("bridge", "timeout", True, "timeout"),
        ("bridge", "security", "plaintext", "security"),
        ("bridge", "certificate", "relative.pem", "certificate"),
        ("bridge", "drafts_mailbox", "Drafts\nInjected", "drafts_mailbox"),
        ("bridge", "password", "do not expose", "unsupported"),
        ("mail", "allowed_senders", [], "non-empty"),
        ("mail", "allowed_senders", "sender@example.com", "array"),
        ("mail", "allowed_senders", ["invalid"], "ASCII mailbox"),
        ("mail", "allowed_senders", ["Name <sender@example.com>"], "ASCII mailbox"),
        ("mail", "allowed_senders", ["x@example.com\r\n"], "ASCII mailbox"),
        ("mail", "allowed_senders", ["ñ@example.com"], "ASCII mailbox"),
        ("mail", "allowed_senders", ["x@example.com", "X@example.com"], "duplicates"),
    ],
)
def test_invalid_settings(settings, table, field, value, error):
    settings[table][field] = value
    with pytest.raises(ConfigurationError, match=error) as exc:
        validate_config(settings)
    assert "do not expose" not in str(exc.value)


def test_rejects_missing_and_invalid_certificate(settings, certificate, tmp_path):
    path, context = certificate
    settings["bridge"]["certificate"] = str(tmp_path / "missing.pem")
    with pytest.raises(ConfigurationError, match="existing absolute PEM file"):
        validate_config(settings)
    settings["bridge"]["certificate"] = str(tmp_path)
    with pytest.raises(ConfigurationError, match="existing absolute PEM file"):
        validate_config(settings)
    settings["bridge"]["certificate"] = str(path)
    context.load_verify_locations.side_effect = ssl.SSLError("private details")
    with pytest.raises(ConfigurationError, match="readable PEM certificate") as exc:
        validate_config(settings)
    assert "private details" not in str(exc.value)


def test_failed_replace_preserves_previous_file(settings, monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("original", encoding="utf-8")

    def fail_replace(source, destination):
        raise PermissionError

    monkeypatch.setattr(os, "replace", fail_replace)
    with (
        pytest.raises(ConfigurationError, match="keyring may already"),
        staged_config(path, validate_config(settings)),
    ):
        assert path.read_text(encoding="utf-8") == "original"
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".config-*")) == []

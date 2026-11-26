from unittest.mock import Mock

import pytest
from keyring.backends.chainer import ChainerBackend

from proton_mail_cli import credentials
from proton_mail_cli.errors import CredentialError


def test_secure_keyring_round_trip(secure_backend):
    credentials.store_password("bridge-user", "test-password")
    secure_backend.set_password.assert_called_once_with(
        "proton-mail-cli", "bridge-user", "test-password"
    )
    assert credentials.get_password("bridge-user") == "test-password"
    secure_backend.get_password.assert_called_once_with(
        "proton-mail-cli", "bridge-user"
    )


def test_rejects_unsupported_backend(monkeypatch):
    backend = Mock()
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: backend)
    with pytest.raises(CredentialError, match="No supported secure system keyring"):
        credentials.store_password("bridge-user", "test-password")
    backend.set_password.assert_not_called()


def test_chainer_does_not_use_plaintext(monkeypatch, secure_backend):
    fallback = Mock()
    chain = ChainerBackend()
    monkeypatch.setattr(ChainerBackend, "backends", [fallback, secure_backend])
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: chain)
    secure_backend.set_password.side_effect = RuntimeError("test-password")
    with pytest.raises(CredentialError, match="Could not store") as exc:
        credentials.store_password("bridge-user", "test-password")
    fallback.set_password.assert_not_called()
    assert "test-password" not in str(exc.value)


def test_missing_password(secure_backend):
    secure_backend.get_password.return_value = None
    with pytest.raises(CredentialError, match="not found"):
        credentials.get_password("bridge-user")


def test_keyring_read_failure_does_not_expose_backend_details(secure_backend):
    secret = "private keyring details"
    secure_backend.get_password.side_effect = RuntimeError(secret)
    with pytest.raises(CredentialError, match="Could not read") as exc:
        credentials.get_password("bridge-user")
    assert secret not in str(exc.value)
    assert exc.value.__suppress_context__

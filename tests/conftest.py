from unittest.mock import Mock

import pytest

from proton_mail_cli import config, credentials


@pytest.fixture
def certificate(tmp_path, monkeypatch):
    path = tmp_path / "bridge.pem"
    path.write_text("placeholder: SSL context is mocked", encoding="utf-8")
    context = Mock()
    monkeypatch.setattr(config.ssl, "SSLContext", Mock(return_value=context))
    return path, context


@pytest.fixture
def secure_backend(monkeypatch):
    backend_type = type(
        "Keyring",
        (),
        {"__module__": "keyring.backends.macOS", "priority": 5},
    )
    backend = backend_type()
    backend.set_password = Mock()
    backend.get_password = Mock(return_value="test-password")
    monkeypatch.setattr(credentials.keyring, "get_keyring", Mock(return_value=backend))
    return backend

"""Store and retrieve Bridge passwords through native secure backends only."""

import keyring
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend

from .errors import CredentialError

SERVICE = "proton-mail-cli"
_SECURE_BACKENDS = {
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.Windows", "WinVaultKeyring"),
    ("keyring.backends.SecretService", "Keyring"),
    ("keyring.backends.kwallet", "DBusKeyring"),
    ("keyring.backends.libsecret", "Keyring"),
}


# Native APIs and backend discovery can raise platform-specific exceptions.
# Translate them at the keyring boundary so their details never reach the CLI.
def _secure_backend() -> KeyringBackend:
    try:
        selected = keyring.get_keyring()
        # Do not delegate to the chainer: it can choose a third-party fallback.
        candidates = (
            selected.backends if isinstance(selected, ChainerBackend) else [selected]
        )
        for backend in candidates:
            cls = type(backend)
            if (
                cls.__module__,
                cls.__name__,
            ) in _SECURE_BACKENDS and backend.priority > 0:
                return backend
    except Exception:  # noqa: BLE001
        raise CredentialError("Could not initialize a secure system keyring.") from None
    raise CredentialError(
        "No supported secure system keyring is available. A plaintext backend is not allowed."
    )


def store_password(username: str, password: str) -> None:
    if not password.strip():
        raise CredentialError("The Bridge password cannot be empty.")
    backend = _secure_backend()
    try:
        backend.set_password(SERVICE, username, password)
    except Exception:  # noqa: BLE001
        raise CredentialError(
            "Could not store the Bridge password in the system keyring."
        ) from None


def get_password(username: str) -> str:
    backend = _secure_backend()
    try:
        password = backend.get_password(SERVICE, username)
    except Exception:  # noqa: BLE001
        raise CredentialError(
            "Could not read the Bridge password from the system keyring."
        ) from None
    if not password:
        raise CredentialError("Bridge password not found; run proton-mail configure.")
    return password

"""Load, validate, and atomically write non-secret Bridge settings."""

import ipaddress
import math
import os
import ssl
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import tomli_w

from .addresses import is_mailbox
from .errors import ConfigurationError
from .models import BridgeConfig

_BRIDGE_KEYS = {
    "host",
    "smtp_port",
    "imap_port",
    "username",
    "security",
    "timeout",
    "certificate",
    "drafts_mailbox",
}


def config_path(override: Path | None = None) -> Path:
    try:
        if override is not None:
            return override.expanduser()
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", ""))
        base = xdg if xdg.is_absolute() else Path.home() / ".config"
        return base / "proton-mail-cli" / "config.toml"
    except RuntimeError:
        raise ConfigurationError(
            "Could not determine the configuration path."
        ) from None


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ConfigurationError(f"{field} must be a non-empty single-line string.")
    return value


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return "%" not in host and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ConfigurationError(f"{field} must be an integer from 1 to 65535.")
    return value


def _timeout(value: object) -> float:
    if type(value) in (int, float):
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds > 0:
                return seconds
        except OverflowError:
            pass
    raise ConfigurationError("bridge.timeout must be a finite positive number.")


def _certificate(value: object) -> Path:
    raw = _text(value, "bridge.certificate")
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ConfigurationError(
                "bridge.certificate must point to an existing absolute PEM file."
            )
        # Validate the trust material without opening a connection to Bridge.
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=path)
        return path
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError(
            "bridge.certificate must be a readable PEM certificate."
        ) from None


def validate_config(data: dict[str, object]) -> BridgeConfig:
    bridge, mail = data.get("bridge"), data.get("mail")
    if not isinstance(bridge, dict) or not isinstance(mail, dict):
        raise ConfigurationError("Configuration must contain [bridge] and [mail].")
    if data.keys() - {"bridge", "mail"} or bridge.keys() - _BRIDGE_KEYS:
        raise ConfigurationError("Configuration contains unsupported settings.")
    if mail.keys() - {"allowed_senders"}:
        raise ConfigurationError("The [mail] table contains unsupported settings.")

    host = _text(bridge.get("host"), "bridge.host")
    if not _is_loopback(host):
        raise ConfigurationError("bridge.host must be localhost or a loopback IP.")
    security = bridge.get("security")
    if security not in ("starttls", "tls"):
        raise ConfigurationError("bridge.security must be 'starttls' or 'tls'.")
    senders = mail.get("allowed_senders")
    if not isinstance(senders, list) or not senders:
        raise ConfigurationError("mail.allowed_senders must be a non-empty array.")
    if not all(isinstance(sender, str) and is_mailbox(sender) for sender in senders):
        raise ConfigurationError("Each allowed sender must be a single ASCII mailbox.")
    if len({sender.casefold() for sender in senders}) != len(senders):
        raise ConfigurationError("mail.allowed_senders must not contain duplicates.")
    mailbox = (
        _text(bridge["drafts_mailbox"], "bridge.drafts_mailbox")
        if "drafts_mailbox" in bridge
        else None
    )
    return BridgeConfig(
        host=host,
        smtp_port=_port(bridge.get("smtp_port"), "bridge.smtp_port"),
        imap_port=_port(bridge.get("imap_port"), "bridge.imap_port"),
        username=_text(bridge.get("username"), "bridge.username"),
        security=security,
        timeout=_timeout(bridge.get("timeout")),
        certificate=_certificate(bridge.get("certificate")),
        allowed_senders=tuple(senders),
        drafts_mailbox=mailbox,
    )


def load_config(path: Path | None = None) -> BridgeConfig:
    path = config_path(path)
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigurationError(
            "Configuration not found; run proton-mail configure."
        ) from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ConfigurationError("Configuration is not valid UTF-8 TOML.") from None
    except (OSError, ValueError):
        raise ConfigurationError("Could not read the configuration file.") from None
    return validate_config(data)


@contextmanager
def staged_config(path: Path, config: BridgeConfig) -> Iterator[None]:
    """Stage a private file, publishing it only if the caller succeeds."""
    bridge = {
        "host": config.host,
        "smtp_port": config.smtp_port,
        "imap_port": config.imap_port,
        "username": config.username,
        "security": config.security,
        "timeout": config.timeout,
        "certificate": str(config.certificate),
    }
    if config.drafts_mailbox is not None:
        bridge["drafts_mailbox"] = config.drafts_mailbox
    data = {"bridge": bridge, "mail": {"allowed_senders": list(config.allowed_senders)}}

    temporary = None
    try:
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".config-", delete=False
            ) as file:
                temporary = Path(file.name)
                tomli_w.dump(data, file)
                file.flush()
                os.fsync(file.fileno())
        except (OSError, ValueError):
            raise ConfigurationError(
                "Could not stage the configuration file."
            ) from None
        yield
        try:
            os.replace(temporary, path)
        except OSError:
            raise ConfigurationError(
                "Could not replace the configuration file; the keyring may already "
                "have been updated."
            ) from None
    finally:
        if temporary is not None:
            _remove_temporary(temporary)


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Do not mask the validation, keyring, or replacement error.
        pass

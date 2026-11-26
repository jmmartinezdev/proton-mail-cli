"""Validation for bare ASCII mailboxes used by configuration and messages."""

import re

_ATOM = r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_PART = re.compile(rf"{_ATOM}(?:\.{_ATOM})*")
_DOMAIN_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")


def is_mailbox(address: str) -> bool:
    local, _, domain = address.partition("@")
    return (
        len(address) <= 254
        and len(local) <= 64
        and _LOCAL_PART.fullmatch(local) is not None
        and all(_DOMAIN_LABEL.fullmatch(label) for label in domain.split("."))
    )

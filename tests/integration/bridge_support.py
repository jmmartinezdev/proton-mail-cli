"""Guard live Bridge tests and generate non-sensitive acceptance fixtures."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise, takewhile
from pathlib import Path

from proton_mail_cli.addresses import is_mailbox

OPT_IN = "PROTON_MAIL_TEST_LIVE"
CONFIG = "PROTON_MAIL_TEST_CONFIG"
SENDER = "PROTON_MAIL_TEST_SENDER"
RECIPIENT = "PROTON_MAIL_TEST_RECIPIENT"


class LiveTestsDisabled(Exception):
    """The operator has not opted into live tests."""


@dataclass(frozen=True)
class LiveSettings:
    config: Path
    sender: str
    recipient: str


def require_live_settings(
    argv: Sequence[str], mark_expression: str, env: Mapping[str, str]
) -> LiveSettings:
    # Inspect invocation arguments as well as the effective expression: a
    # pytest config file or PYTEST_ADDOPTS alone is not explicit selection.
    options = tuple(takewhile(lambda arg: arg != "--", argv))
    selected = (
        ("-m", "integration") in pairwise(options)
        or "-mintegration" in options
        or "-m=integration" in options
    )
    if not selected or mark_expression != "integration":
        raise LiveTestsDisabled("Live Bridge tests require explicit -m integration.")
    if env.get(OPT_IN) != "1":
        raise LiveTestsDisabled(f"Live Bridge tests require {OPT_IN}=1.")

    missing = [name for name in (CONFIG, SENDER, RECIPIENT) if not env.get(name)]
    if missing:
        raise ValueError(f"Set the required test variables: {', '.join(missing)}.")
    config = Path(env[CONFIG])
    if not config.is_absolute():
        raise ValueError(f"{CONFIG} must be an absolute path to the test config.")
    for name in (SENDER, RECIPIENT):
        if not is_mailbox(env[name]):
            raise ValueError(f"{name} must be a single bare ASCII mailbox.")
    return LiveSettings(config, env[SENDER], env[RECIPIENT])


def synthetic_pdf() -> bytes:
    """Return a deterministic one-page PDF with a valid cross-reference table."""
    content = b"BT /F1 14 Tf 24 72 Td (Proton Mail CLI Phase 6 test) Tj ET\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 100] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, data in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + data + b"\nendobj\n")
    xref = len(pdf)
    size = len(objects) + 1
    pdf.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(pdf)

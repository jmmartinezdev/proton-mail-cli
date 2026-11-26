"""Exercise the production CLI against a deliberately selected test account."""

import os
from hashlib import sha256
from uuid import uuid4

import pytest

from proton_mail_cli.cli import main

from .bridge_support import LiveTestsDisabled, require_live_settings, synthetic_pdf

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def live_settings(pytestconfig):
    try:
        return require_live_settings(
            pytestconfig.invocation_params.args,
            pytestconfig.getoption("markexpr"),
            os.environ,
        )
    except LiveTestsDisabled as error:
        pytest.skip(str(error))
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)


@pytest.mark.parametrize("operation", ["send", "draft"])
@pytest.mark.parametrize("case", ["plain", "html", "dry-run", "invalid"])
def test_cli_acceptance(operation, case, live_settings, tmp_path):
    subject = f"Phase 6 CLI {operation}-{case} {uuid4().hex}"
    plain = tmp_path / "body.txt"
    plain.write_text("Phase 6 test: café — 世界\n", encoding="utf-8")
    pdf = synthetic_pdf()
    attachment = tmp_path / "phase6-test.pdf"
    attachment.write_bytes(pdf)

    body = plain
    extra = []
    if case == "html":
        body = tmp_path / "body.html"
        body.write_text(
            "<p><strong>Phase 6 test:</strong> café — 世界</p>", encoding="utf-8"
        )
        extra = ["--plain-body", str(plain)]
    elif case == "dry-run":
        extra = ["--dry-run"]
    elif case == "invalid":
        body = tmp_path / "missing.txt"

    # Print only the synthetic search token and checksum, before any submission.
    print(f"\nSearch subject: {subject}", flush=True)
    print(f"{attachment.name} SHA-256: {sha256(pdf).hexdigest()}", flush=True)
    status = main(
        [
            "--config",
            str(live_settings.config),
            operation,
            "--from",
            live_settings.sender,
            "--to",
            live_settings.recipient,
            "--subject",
            subject,
            "--body",
            str(body),
            "--attach",
            str(attachment),
            *extra,
        ]
    )
    expected = 2 if case == "invalid" else 0
    if status != expected:
        pytest.fail(
            f"{operation}-{case}: expected CLI exit {expected}, got {status}. "
            "Check the CLI diagnostic and do not rerun uncertain submissions.",
            pytrace=False,
        )

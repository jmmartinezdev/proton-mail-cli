# Proton Mail CLI — Implementation plan

## Development checklist

**Current state:** Phases 0 through 7 are complete.

- [x] Phase 0 — Bridge compatibility: maintainer verified sending and drafting with the local Bridge.
- [x] Phase 1 — Project skeleton and configuration: automated checks pass; manual system-keyring verification is documented in the README.
- [x] Phase 2 — Message construction
- [x] Phase 3 — SMTP adapter
- [x] Phase 4 — IMAP adapter
- [x] Phase 5 — CLI and orchestration
- [x] Phase 6 — Tests and acceptance: offline checks passed and the maintainer verified the live run.
- [x] Phase 7 — Packaging and documentation

## 1. Objective and scope

Build a synchronous Python 3.13 command-line application that sends email or creates editable drafts in Proton Mail through a locally running Proton Mail Bridge. Development tools and Bridge are already installed. Use uv, argparse, standard-library SMTP/IMAP/MIME support, keyring, pytest and Ruff. Target macOS first while keeping the code portable to Linux and Windows.

MVP: one sender, one recipient, subject, UTF-8 body file, zero or more attachments per invocation; multiple configured sender identities; plain text and HTML; configuration and dry-run. No scheduling, batch sending, templates, direct SMTP backend, or draft editing in this release.

## 2. CLI contract

```bash
proton-mail configure
proton-mail send --from billing@example.com --to recipient@example.net \
  --subject 'Invoice' --body body.txt --attach invoice.pdf --attach details.pdf
proton-mail draft --from billing@example.com --to recipient@example.net \
  --subject 'Invoice' --body body.html --attach invoice.pdf
proton-mail send --from billing@example.com --to recipient@example.net \
  --subject 'Test' --body body.txt --dry-run
```

- `send` and `draft` require `--from`, `--to`, `--subject`, `--body`; `--attach` is repeatable.
- `.txt` means plain text; `.html` means HTML; reject other extensions for MVP. For HTML, generate a reasonable plain-text alternative using a small HTML parser or require an optional `--plain-body` file; choose the latter for predictable output, with HTML-only allowed when absent.
- `--config PATH` overrides the default config location; `--dry-run` validates and builds the message without credentials or network access, printing only metadata and attachment sizes, not body content.
- `configure` prompts for settings and password using getpass, validates configuration and stores the password securely. Never send a message during configuration.
- Output a concise success message to stdout; errors to stderr. No interactive prompts in send/draft. Use `--help` for documentation.

## 3. Architecture and repository

```text
proton-mail-cli/
  pyproject.toml
  uv.lock
  README.md
  .gitignore
  src/proton_mail_cli/
    __init__.py
    cli.py
    models.py
    config.py
    credentials.py
    message_builder.py
    mail_service.py
    smtp_client.py
    imap_client.py
    errors.py
  tests/
    test_cli.py
    test_config.py
    test_message_builder.py
    test_mail_service.py
    test_smtp_client.py
    test_imap_client.py
    integration/test_bridge.py
```

Dependency direction: CLI -> mail service -> message builder and transport adapters. Configuration and credential retrieval are injected at the composition root. Keep protocol operations separate from file parsing. Use dataclasses for `MessageRequest`, `BridgeConfig` and `OperationResult`; use pathlib paths and typed functions. No DI framework or async runtime.

Public internal interfaces:

```python
build_message(request: MessageRequest) -> EmailMessage
send_message(message: EmailMessage, config: BridgeConfig, password: str) -> OperationResult
save_draft(message: EmailMessage, config: BridgeConfig, password: str) -> OperationResult
```

Define a console entry point in pyproject.toml: `proton-mail = "proton_mail_cli.cli:main"`. `main()` returns an integer exit status.

## 4. Phase 0 — Prove Bridge compatibility before building the CLI

This is the critical technical gate. Use a disposable test recipient/account or your own address. Create a small temporary integration script, not production code.

1. Read actual host, ports, username, password and security mode from Bridge. Defaults are 127.0.0.1, SMTP 1025 and IMAP 1143; do not assume these values are unchanged.
2. Export the Bridge TLS certificate and create an SSL context trusting it. Verify the hostname/IP matches its certificate. If not, investigate the certificate and supported connection hostname; do not disable certificate validation.
3. Authenticate to SMTP using STARTTLS (EHLO, STARTTLS, EHLO, login) or implicit TLS if configured. Send a uniquely identified test message to yourself with a PDF attachment.
4. Authenticate to IMAP using STARTTLS or implicit TLS as configured. Run LIST and identify the mailbox with the special-use `\\Drafts` attribute; if unavailable, use an explicitly configured mailbox name verified by LIST.
5. Construct a MIME message with a different unique subject and append it to the draft mailbox using `IMAP4.append(mailbox, r'(\Draft)', None, message.as_bytes(policy=SMTP))`. Check the tagged response for OK.
6. Open Proton Mail web UI, verify the draft appears, is editable, retains sender, recipient, body and attachment, and can be sent manually. Repeat with HTML and each custom-domain sender you intend to use. Check synchronization and Sent behavior.
7. Record results in README without credentials. If combined-address mode does not preserve the requested sender, test split-address mode and per-identity Bridge credentials before committing to configuration design.

**Gate:** do not advertise draft support until the web UI test passes. IMAP APPEND acceptance alone is insufficient. If it fails, isolate the issue and retain sending support; a local EML export is a possible separately approved fallback, not an equivalent Proton draft.

## 5. Phase 1 — Project skeleton and configuration

Initialize a package if not already created: `uv init --package proton-mail-cli`; pin Python 3.13 and add `keyring`, plus dev dependencies `pytest` and `ruff`. Set up the console entry point and pytest integration marker. Commit the lockfile.

Default configuration: `~/.config/proton-mail-cli/config.toml` (honor XDG_CONFIG_HOME where set; use an explicit override on other platforms if needed).

```toml
[bridge]
host = "127.0.0.1"
smtp_port = 1025
imap_port = 1143
username = "bridge-username"
security = "starttls"
timeout = 30
certificate = "/absolute/path/to/bridge-cert.pem"
# drafts_mailbox = "Drafts" # explicit override only if needed

[mail]
allowed_senders = ["billing@example.com", "personal@example.com"]
```

Read TOML with tomllib. For configure, use a small TOML writer dependency (tomli-w) rather than hand-escaping values; add it only when implementing this command. Validate port ranges, timeout, TLS mode, certificate path and allowed senders. Restrict Bridge connections to loopback in MVP. Store password in keyring under service `proton-mail-cli` and Bridge username; fail clearly if no secure backend is available. Do not silently use plaintext fallback. Use atomic configuration writes and restrictive permissions where supported. Do not commit credentials, local config, certificates or real test messages. If split-address mode is required, extend configuration to map each sender to its own Bridge profile.

## 6. Phase 2 — Message construction

- Validate exactly one mailbox per sender/recipient, reject malformed addresses and CR/LF in headers; validate sender against configured identities. Support normal ASCII mailbox addresses and Unicode subject/body; internationalized mailbox addresses are outside MVP unless explicitly tested.
- Validate all files exist, are readable regular files, and have supported body extension. Read body with UTF-8 and fail on decoding errors. Check attachment sizes before reading; enforce a configurable conservative total size limit accounting for MIME/base64 overhead and actual Proton limits.
- Build with `email.message.EmailMessage(policy=email.policy.SMTP)`; set From, To, Subject, Date and Message-ID. Use `set_content` for plain text and `add_alternative` for HTML when a plain alternative is supplied; HTML-only uses `set_content(..., subtype="html")`.
- For each attachment, use `mimetypes.guess_type`, fall back to application/octet-stream, and `add_attachment` with bytes and basename only. Preserve original bytes and attachment order.
- Do not manually assemble MIME boundaries or encode headers. Serialize and parse back in tests to verify content and attachment bytes.

## 7. Phase 3 — SMTP adapter

Use smtplib with an explicit timeout and SSL context created from the trusted Bridge certificate. For STARTTLS, call EHLO before and after upgrading; never authenticate before TLS. For implicit TLS use SMTP_SSL. Authenticate with Bridge credentials, then call `send_message` with explicit envelope sender and recipient. Treat recipient refusal as failure. Close connection reliably using context management or finally.

Distinguish authentication, TLS, connection and SMTP rejection errors. A disconnect after message submission begins may mean the server accepted the message; report an uncertain outcome rather than automatically retrying. SMTP acceptance means accepted by Bridge, not confirmed recipient delivery. Do not append a second Sent copy unless integration testing proves it is necessary.

## 8. Phase 4 — IMAP adapter

Use imaplib with timeout, trusted TLS context and Bridge credentials. Discover the Drafts mailbox using LIST special-use attributes or the verified override. Handle quoted mailbox names correctly; fail clearly if missing or ambiguous. Append serialized SMTP-policy MIME bytes with the Draft flag. Check response status; optionally record APPENDUID if returned, without requiring it. Do not select or modify unrelated mailboxes. Logout in finally without masking the original error.

A successful APPEND means Bridge accepted the message, not necessarily that cloud synchronization has completed. Report uncertain outcome on disconnect during APPEND; do not automatically retry. Reuse Phase 0 findings to handle identity mode and draft semantics.

## 9. Phase 5 — CLI and orchestration

Implement argparse subcommands and common arguments. Convert parsed inputs into MessageRequest. Validate configuration and sender, build the MIME message, then retrieve credentials only for real operations. Dry-run must not invoke keyring or network clients. Dispatch to the appropriate adapter; never fall back from draft to send. Print metadata-only results and map domain exceptions to exit codes.

| Code | Meaning |
|---|---|
| 0 | Success or valid dry-run |
| 1 | Unexpected internal failure |
| 2 | Invalid arguments or input files |
| 3 | Configuration or credential failure |
| 4 | Connection or TLS failure |
| 5 | Authentication failure |
| 6 | Server rejection or mailbox failure |
| 7 | Uncertain send/append outcome; verify before retrying |

Never log passwords, full message contents or attachment bytes. Avoid recipient/subject logging by default. Keep traceback output opt-in and sanitized.

## 10. Phase 6 — Tests and acceptance

Unit tests must not require Bridge or send real mail. Use pytest fixtures, tmp_path and unittest.mock; inject transport factories where useful.

| Area | Required cases |
|---|---|
| CLI | Required flags, repeated attachments, invalid arguments, dry-run no network, dispatch and exit codes |
| Configuration | Missing/invalid TOML, overrides, sender allowlist, unavailable keyring, certificate validation |
| MIME | Unicode, plain text, HTML, alternative body, no/multiple PDFs, unknown attachment type, exact attachment bytes, missing files, header injection |
| SMTP | TLS before login, envelope addresses, refusal, authentication failure, timeout, uncertain outcome, cleanup |
| IMAP | Draft discovery, override, quoted mailbox, Draft flag, MIME bytes, rejected APPEND, uncertain outcome, cleanup |
| Service | Validation before transport, no send on draft, errors mapped correctly |

Integration tests are opt-in (`pytest -m integration` with an additional explicit environment opt-in), use a designated test recipient and unique subjects, and never run in normal CI. Verify actual delivery, Sent folder, editable web drafts, custom-domain identities, Unicode and PDF integrity. Check that failed or dry-run operations create no messages. Document manual cleanup of test messages.

**Acceptance record:** All 170 offline tests and all 8 live integration cases
passed. The maintainer confirmed the test emails and drafts were correct and
the downloaded PDF checksums matched. The live run used Bridge on macOS.

## 11. Phase 7 — Packaging and documentation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -m 'not integration'
uv run proton-mail --help
uv tool install .
```

Document Bridge startup/login requirements, certificate export and rotation, configure workflow, examples, exit codes, troubleshooting and safe retries. Test installation outside the repository. No PyInstaller needed initially. Add CI for unit tests and linting on macOS, Linux and Windows; distinguish portability of code from verified Bridge integration on each OS.

**Verification record:** On macOS with Python 3.13, the lockfile check, Ruff
lint and formatting checks, all 170 offline tests, and CLI help passed. An
isolated uv tool install and reinstall passed, and the installed entry point
and all three subcommand help screens worked from outside the checkout.
The maintainer confirmed that all three hosted GitHub Actions jobs passed:
macOS, Linux, and Windows. These were offline checks; live Bridge acceptance
was verified on macOS only.

## Definition of done

- Installed CLI runs from any directory and exposes documented commands.
- Sends plain text and HTML with zero or multiple attachments from configured custom-domain addresses.
- Drafts appear in Proton Mail web, remain editable and can be sent with correct sender and intact attachments.
- Credentials are stored securely, TLS is verified, and no secrets are committed or printed.
- Dry-run performs no network operations; errors return documented exit codes; uncertain outcomes are never retried automatically.
- Unit tests and linting pass; opt-in real Bridge tests and manual acceptance checks are documented.

## Decisions and outstanding risks

The plan assumes a local Bridge, synchronous execution and Python 3.13. The maintainer verified sending and drafting with the installed Bridge in Phase 0 and confirmed that one shared Bridge profile is sufficient. Detailed per-sender results are not recorded here. Cross-platform operation requires a running Bridge and a usable secure keyring backend on each target OS. Scheduling is intentionally external and deferred.

## References

- https://proton.me/mail/bridge
- https://proton.me/support/mail/bridge
- https://docs.python.org/3/library/smtplib.html
- https://docs.python.org/3/library/imaplib.html
- https://docs.python.org/3/library/email.message.html
- https://docs.python.org/3/library/ssl.html
- https://docs.astral.sh/uv/
- https://keyring.readthedocs.io/

# Proton Mail CLI

Send email or save an editable draft through a locally running Proton Mail
Bridge. Each command uses one sender and one recipient.

## Install or upgrade

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
these commands from the repository root:

```text
uv tool install --python 3.13 .
proton-mail --help
```

If your shell cannot find `proton-mail`, run `uv tool update-shell`, open a new
terminal, and try again. Once installed, run `proton-mail` from any directory.
Relative paths for message files are resolved from your current directory.

To upgrade from an updated checkout, run:

```text
uv tool install --python 3.13 --reinstall-package proton-mail-cli .
```

The CLI configuration and keyring entry are separate from the installed package.
From a checkout, you can also use `uv run proton-mail --help` without installing
it as a tool.

## Set up Proton Mail Bridge

1. Install and open [Proton Mail Bridge](https://proton.me/mail/bridge), sign in,
   and wait for it to sync. Keep Bridge running for real sends and drafts. The
   CLI connects only to the local Bridge, not directly to Proton Mail.
2. In Bridge, find the selected account's local host, SMTP and IMAP ports,
   TLS mode, username, and password. The CLI uses one shared Bridge profile:
   use the settings shown by Bridge, not assumed defaults. Use the Bridge-issued
   password, **not** your Proton account password.
3. In Bridge, go to **Settings → Advanced settings → Export TLS certificates**
   ([Proton’s guide](https://proton.me/support/comprehensive-guide-to-bridge-settings)).
   Save the **public PEM certificate** in a private directory outside the
   checkout. The CLI only needs that certificate, not the exported private key.
   Do not commit credentials, certificates, or the private key.
4. Use a loopback host covered by the certificate. `127.0.0.1` needs a matching
   IP subject alternative name (SAN); `localhost` needs a matching DNS SAN.
   If you have OpenSSL, inspect the certificate names and validity dates with
   `openssl x509 -in bridge-cert.pem -noout -dates -ext subjectAltName`.

If Bridge regenerates its certificate or the old one expires, re-export the
public PEM, verify the new certificate matches the Bridge host, and run
`proton-mail configure` again with the new path and current Bridge settings.
Do not disable certificate or hostname verification.

## Configure credentials

Run from an interactive terminal:

```text
proton-mail configure
```

Enter the Bridge settings, the absolute public PEM path, and a comma-separated
list of allowed sender addresses. Choose `starttls` for STARTTLS or `tls` for
implicit TLS. Leave the Drafts mailbox override blank unless automatic discovery
fails and you have confirmed the exact selectable IMAP mailbox name.

`configure` validates the settings without contacting Bridge. It writes a
password-free TOML file at `~/.config/proton-mail-cli/config.toml`, or under
`$XDG_CONFIG_HOME/proton-mail-cli/` if `XDG_CONFIG_HOME` is absolute. To use a
different file, pass `--config PATH` before or after the subcommand.

The hidden password prompt saves the Bridge password separately under the
`proton-mail-cli` service and Bridge username. The CLI requires a supported
system keyring (such as macOS Keychain, Windows Credential Locker, or Linux
Secret Service or KWallet). If none is available, configuration fails rather
than storing a plaintext password.

## Send, draft, or preview

Substitute your configured sender and local message files:

```text
proton-mail send --from billing@example.com --to recipient@example.net --subject "Invoice" --body body.txt --attach invoice.pdf
proton-mail draft --from billing@example.com --to recipient@example.net --subject "Invoice" --body body.html --plain-body body.txt
proton-mail send --from billing@example.com --to recipient@example.net --subject "Invoice" --body body.txt --attach invoice.pdf --dry-run
```

Use a UTF-8 `.txt` or `.html` body. For HTML, `--plain-body` adds an optional
UTF-8 `.txt` alternative. Repeat `--attach` for multiple files. The default
limit is **20 MiB (20,971,520 bytes)** for the entire SMTP-formatted message,
including headers and MIME-encoded attachments. Bridge or Proton Mail may
impose other limits.

Both commands accept `--dry-run`, which validates the local settings and
files and prints only message and attachment sizes. It does not look up the
password or contact Bridge. Without `--dry-run`, `send` submits an email and
`draft` saves a draft without sending it. Success means **Bridge accepted the
operation**, not that the recipient received the email or that the draft
appeared in the web UI.

## Exit codes and troubleshooting

| Code | Meaning and next step |
| --- | --- |
| `0` | Successful operation or dry run. Check the recipient or web Drafts if you need to confirm delivery or sync. |
| `1` | Unexpected internal failure. Keep the command and diagnostic for troubleshooting, but remove private details and credentials before sharing them. |
| `2` | Invalid arguments or input files. Check the command's `--help`, the sender allowlist, and the file paths, encoding, or size. |
| `3` | Configuration or credential failure. Check `--config`, rerun `proton-mail configure` if needed, and make sure the system keyring is available and unlocked. |
| `4` | Connection or TLS failure. Make sure Bridge is running, then check the loopback host, ports, TLS mode, and public certificate. Re-export it if Bridge rotated it. |
| `5` | Bridge authentication failed. Confirm that Bridge is signed in and configure the username and password shown by Bridge. |
| `6` | Server rejection or mailbox failure. Check that Bridge accepts the sender and recipient. For drafts, use automatic discovery or provide a verified selectable IMAP mailbox override. |
| `7` | Uncertain submission outcome. Check before retrying; Bridge might already have accepted it. |

For an uncertain **send**, check Proton Mail **Sent** and the recipient’s
mailbox. For an uncertain **draft**, check Proton Mail web **Drafts** after
allowing time to sync. If you still cannot tell whether the operation succeeded,
do not submit it again blindly. The CLI never retries automatically.

## Platform verification

The GitHub Actions workflow is configured to run lint, formatting, offline
tests, and the CLI help check on macOS, Linux, and Windows with Python 3.13.
These checks do not require Bridge or a keyring and do not verify live
compatibility. The maintainer has verified live Bridge sending and drafting on
**macOS**; live Linux and Windows operation has not been verified.

## Phase 6: CLI acceptance against a live Bridge

Ordinary test runs are offline. To run acceptance, start and sign in to Bridge,
use an explicit test config and its secure keyring entry, and choose a test
recipient whose mailbox you control. The command below submits **two real
emails and two real drafts** per sender on a successful run. The dry-run and
invalid-input cases do not submit anything.

From the repository root, replace the sample settings and run:

```sh
PROTON_MAIL_TEST_LIVE=1 \
PROTON_MAIL_TEST_CONFIG="$HOME/path/to/test-config.toml" \
PROTON_MAIL_TEST_SENDER="billing@example.com" \
PROTON_MAIL_TEST_RECIPIENT="test-recipient@example.net" \
  uv run pytest -m integration -s -x
```

The suite requires both explicit `-m integration` and `PROTON_MAIL_TEST_LIVE=1`.
No password is passed in the environment; the CLI uses the configured account's
keyring entry. Repeat with each configured custom-domain sender. Each case
prints a unique `Phase 6 CLI …` search subject and the SHA-256 of its synthetic
`phase6-test.pdf`. `-x` stops the run at the first failure. If a submission's
outcome is uncertain, search for that exact subject before rerunning.

Passing tests confirm Bridge acceptance for live cases, **not** delivery or
web sync. Complete these manual checks for each run:

1. **`send-plain` / `send-html`:** Find each exact subject in the recipient's
   mailbox and confirm exactly one corresponding copy in Proton Mail web
   **Sent**. Check the actual From and To addresses, `café — 世界`, the HTML
   formatting for `send-html`, and the attachment.
2. **`draft-plain` / `draft-html`:** Find each exact subject in Proton Mail web
   **Drafts**. Open it in the composer, verify sender, recipient, Unicode,
   formatting, and attachment, then edit the body, close, and reopen the draft
   to confirm the edit persists. Confirm it did not appear in Sent or at the
   recipient. Leave it unsent for cleanup.
3. **PDF integrity:** Open each downloaded `phase6-test.pdf` and run
   `shasum -a 256 /path/to/downloaded/phase6-test.pdf`. Compare it with the
   checksum printed for that case.
4. **`dry-run` / `invalid`:** Search Drafts, Sent, and the recipient's mailbox.
   None of these subjects should appear. The dry run must return `0`; the
   missing body file must return `2`.
5. **Record and clean up:** Record the Bridge version, TLS mode, sender label,
   and which checks passed, without real addresses or credentials. Manually
   remove only the test items identified by their exact subjects from Drafts,
   Sent, and the recipient's mailbox. The suite never sends a draft or deletes
   mail.

The maintainer confirmed Phase 6 acceptance on macOS: all eight live cases
passed, the test emails and drafts were correct, and the PDF checksums matched.

## Phase 0: Bridge compatibility probe

This phase provides a standalone probe, not the production CLI. The `smtp`
command sends **one real email**; the `imap` command appends **one draft** and
does not send it. Use your own address or a disposable test recipient, and a
small, non-sensitive PDF that you can open before and after the test.

### Prepare Bridge

1. Open Proton Mail Bridge, sign in, and let it finish syncing. In Bridge,
   select your account and read the host, port, connection security, username,
   and password shown for **each** protocol. Use the Bridge-issued credentials,
   not your Proton account password. Start in combined-address mode.
2. In Bridge, go to **Settings → Advanced settings → Export TLS certificates**
   ([Proton’s guide](https://proton.me/support/comprehensive-guide-to-bridge-settings)).
   Export to a private directory **outside this repository**. Bridge also exports
   a private key; the probe needs only the public certificate in PEM format.
   Keep the private key private. Do not put credentials, certificates, or test
   messages/PDFs in the repository.
3. Inspect the public certificate and check that the host you will pass is
   covered by it:

   ```sh
   openssl x509 -in "$HOME/path/to/bridge-cert.pem" -noout -subject -dates -ext subjectAltName
   ```

   An IP address such as `127.0.0.1` needs a matching IP SAN. Use `localhost`
   only if the certificate covers that name and Bridge is reachable there. The
   probe accepts only `localhost` or loopback IPs and verifies the certificate
   and server identity during TLS setup. If that fails, check the host, security
   mode, and certificate dates; re-export if Bridge has regenerated the
   certificate. Do not bypass verification. If no supported local host matches,
   stop and record the TLS blocker.

### Run the probes

From the repository root, use Python 3.13 via `uv`. Replace the example
addresses, paths, host, ports, and security modes with your actual settings.
`starttls` means STARTTLS; `tls` means implicit SSL/TLS. Run in an interactive
terminal; enter the Bridge credentials when prompted (the password is not echoed).
The probe uses only the standard library. If `uv` is unavailable, replace
`uv run --no-project --python 3.13 python` with `python3.13`.

```sh
uv run --no-project --python 3.13 python scripts/bridge_probe.py smtp \
  --host 127.0.0.1 --port 1025 --security starttls \
  --cert "$HOME/path/to/bridge-cert.pem" \
  --from billing@example.com --to test-recipient@example.net \
  --pdf "$HOME/path/to/test.pdf" --format plain

uv run --no-project --python 3.13 python scripts/bridge_probe.py imap \
  --host 127.0.0.1 --port 1143 --security starttls \
  --cert "$HOME/path/to/bridge-cert.pem" \
  --from billing@example.com --to test-recipient@example.net \
  --pdf "$HOME/path/to/test.pdf" --format plain
```

Each run prints a unique subject. On success, SMTP reports that Bridge accepted
the submission, **not** that the recipient received it. IMAP reports that
Bridge accepted `APPEND` with `\Draft`, **not** that the web composer can open it.
The IMAP probe uses the selectable mailbox tagged `\Drafts` in `LIST`. If it
cannot find a unique one, it lists selectable mailbox names and exits without
appending. Identify the actual Drafts mailbox and rerun with
`--drafts-mailbox 'Exact listed name'` (not a guessed folder name).

Repeat **both** probes with `--format html` (HTML with a plain-text alternative)
and both formats for **each custom-domain sender** you intend to use. If
combined-address mode rewrites or rejects the sender, repeat in split-address
mode with the Bridge settings and credentials for that specific identity.
Neither probe retries automatically. If a connection drops during submission,
use the printed subject to check Proton Mail and the recipient before retrying.

### Verify manually in Proton Mail

- **SMTP:** Find the exact subject at the recipient and in Proton Mail web
  **Sent**. Confirm there is one Sent copy and the actual From address is the
  requested custom-domain address, not the primary account address. Verify the
  recipient, Unicode text (`café`), and HTML formatting where applicable.
  Download the PDF, check its filename, and open it. To confirm byte integrity,
  compare `shasum -a 256` of the downloaded PDF and the original.
- **IMAP:** Allow time for synchronization, then find the exact subject in
  Proton Mail web **Drafts**. Open it in the composer, verify From, To, subject,
  body, formatting and PDF filename/content, then edit the body, close the
  composer and reopen it to confirm the edit persists. Send it manually.
  Confirm it no longer appears in Drafts, appears once in Sent, and arrives
  with the requested From address, edit and intact PDF.
- After recording results, remove the test drafts and messages from Drafts,
  Sent, and the test recipient’s mailbox as applicable.

### Phase 0 retest template

The maintainer verified sending and drafting with the local Bridge and confirmed
that one shared Bridge profile is sufficient. The table below is a template for
detailed retests; it does not record results for individual senders or formats.
For each run, record the Bridge version, address mode, sender label, format,
TLS mode(s), and results. Omit real email addresses, credentials and private
message details.

| Scenario | Bridge acceptance | Web UI / recipient verification |
| --- | --- | --- |
| SMTP, plain and HTML, each custom-domain sender | Pending | Delivery, sender, PDF, Sent copy pending |
| IMAP, plain and HTML, each custom-domain sender | Pending | Sync, edit, manual send, delivery, Sent copy pending |
| Split-address mode, if needed | Not tested | Retest affected sender(s) |

For future retests, do not count `APPEND OK` alone as proof of draft support.
If web validation fails, record where it failed and isolate the issue; retain
any verified SMTP result separately.

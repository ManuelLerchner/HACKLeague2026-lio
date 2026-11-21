# Live connection checks

The normal push/PR suite uses fixtures and a disposable PostgreSQL service. It does not need
provider credentials. Live checks are separate because provider availability, rate limits and
credentials change independently of application correctness.

## GitHub setup

Use the **Dev** environment in repository **Settings → Environments**. Restrict it
to the default branch. The workflow also rejects other branches and never runs on pull requests.
Environment approval requirements, if configured, apply before credentials become available.

Add these environment secrets:

| Secret | Value |
| --- | --- |
| `PORTAL_TEAM_TOKEN` | Team portal token |
| `PORTAL_CONTRACT_JSON` | Contents of the privately reviewed portal contract JSON |
| `SMTP_PASSWORD` | Mailbox app password |
| `IMAP_PASSWORD` | Mailbox app password |
| `LLM_API_KEY` | Optional OpenAI key; required when the LLM check is selected |
| `LIVE_DATABASE_URL` | Optional deployed PostgreSQL URL; required for deployment checks |

Add environment variables `PORTAL_BASE_URL`, `MAIL_FROM`, `SMTP_HOST`, `SMTP_USER`, `IMAP_HOST`,
and `IMAP_USER`. Ports default to SMTP STARTTLS 587 and IMAP TLS 993; override with `SMTP_PORT`
and `IMAP_PORT`. For Gmail use `smtp.gmail.com` and `imap.gmail.com`; both usernames are the full
mailbox address. `LLM_MODEL` and `LIVE_APP_URL` configure the optional checks.

The six connection variables can also be stored as secrets in `Dev`; the workflow accepts
either form, with variables taking precedence. Credential secrets may be repository-level
or environment-level; environment secrets override repository secrets of the same name.

Run **Actions → live connections → Run workflow** on the default branch. Portal, SMTP and IMAP
are required: missing settings fail rather than silently passing. Select the additional LLM or
deployment checks only after configuring their settings. To also run after pushes to `main`, set
the **repository** variable `RUN_LIVE_CHECKS=true`. Environment variables `CHECK_LLM=true` and
`CHECK_DEPLOYMENT=true` opt those checks into automatic runs.

Secrets are exposed only to the connection-test step. No private portal files, mailbox contents,
credentials or diagnostic exception bodies are uploaded as artifacts or written to test output.
Use a dedicated test mailbox and a read-only database user for deployed connection tests.

## What this verifies

- Portal: HTTPS authentication, reviewed OpenAPI checksum and file-list schema.
- SMTP: TLS connection, login and NOOP. No MAIL FROM, recipient or DATA commands.
- IMAP: TLS login and read-only INBOX selection. No messages downloaded or marked read.
- Optional LLM: configured model visibility. This does not prove structured generation works.
- Optional deployment: PostgreSQL `SELECT 1` in a read-only transaction, API health, database
  readiness and worker heartbeat. No migration or write runs against the deployed database.

This is connection verification, not proof of supplier registration or delivery. A live supplier
exchange is a separate, explicitly requested manual test with a verified portal contact and a
saved send attempt. It must not run for every push or PR.

## Local commands

```sh
pixi run test                    # live tests skipped, even when .env has credentials
RUN_LIVE_TESTS=1 pixi run test-live
RUN_LIVE_TESTS=1 CHECK_LLM=true pixi run test-live
```

Local live checks read `.env` and the private `PORTAL_CONTRACT_PATH`. GitHub uses
`PORTAL_CONTRACT_JSON` instead, so no contract file or credential needs to be committed.
Keep `DRY_RUN=true` and `OUTBOUND_MAIL_ENABLED=false`; connection checks do not change them.

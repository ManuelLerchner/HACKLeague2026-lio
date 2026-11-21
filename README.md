# Supplier Chaser

A procurement inbox that turns missing or inconsistent supplier commitments into targeted
questions, tracks replies, and keeps the evidence behind each next action.

Python modular monolith: FastAPI/Jinja UI and a separate durable worker share PostgreSQL.
The complete offline workflow uses synthetic source files and a deterministic mailbox.
No external supplier is contacted by default.

## Run locally

```sh
pixi install --locked
pixi run db-local                 # Docker alternative: docker compose up -d db
pixi run db-upgrade
pixi run demo-seed
```

Start `pixi run api` and `pixi run worker` in separate terminals. Open http://127.0.0.1:8000.
Sign in using the generated `ADMIN_TOKEN` in `.env`. Do not share or commit that file.

The Docker alternative requires private `POSTGRES_PASSWORD` and `COMPOSE_DATABASE_URL`
settings first; see the [Compose setup](docs/demo-runbook.md#setup). No database password
fallback is supplied in Compose.

`demo-seed` creates synthetic input files, not hardcoded database cases. The worker detects files,
parses orders and confirmations, opens cases, drafts multilingual questions and sends them to
the local fixture outbox. To introduce a changed file and reply:

```sh
pixi run python -m supplier_chaser.demo amend
pixi run python -m supplier_chaser.demo reply
```

Wait for one poll between steps (60 seconds by default), then refresh the case. For automated
replay on a fresh demo database: `pixi run demo-replay`.

## Verify

```sh
pixi run lint
pixi run typecheck
pixi run test
pixi run test-freeze
```

Tests use synthetic fixtures and no external provider credentials. PostgreSQL concurrency tests
run when `TEST_POSTGRES_URL` points to a disposable database; CI provides one. Review snapshots
before approving `pixi run pytest --snapshot-update -m freeze`; CI never updates snapshots.

## Behavior and boundaries

- Append-only file versions and supplier claims; current PO projection with explicit versions.
- Missing, partial, late, amended and contradictory confirmations; unknowns remain review items.
- Stable case keys, thread IDs, send intent, reply deduplication and recoverable job leases.
- At most one automatic reminder, then escalation. Uncertain sends never retry automatically.
- Private evidence, authenticated API, CSRF-protected browser actions and exact recipient checks.
- No ERP writes, physical receipt inference, contracting, spending, transport booking or releases.

**Portal authentication, OpenAPI, file listing and one file download have been verified.**
Gmail SMTP authentication, read-only IMAP access and one threaded challenge-supplier exchange
have also been verified. Live LLM extraction and public deployment remain unverified. The running demo still
uses fixtures. The inspected genuine PO export needs further unit/date mapping before import;
PP-07 and launch-workbook interpretation are not claimed.

An opt-in [live connection workflow](docs/live-checks.md) uses GitHub environment secrets to
check external connections without sending mail. Normal push/PR tests remain offline.

See [assumptions](docs/assumptions.md), [integration contracts](docs/integration-contracts.md),
and the [exact demo/deployment procedure](docs/demo-runbook.md).

`pixi.toml` owns dependencies; `pyproject.toml` owns packaging and tool configuration.
`pixi.lock` covers Linux x86_64 and macOS Apple Silicon. Runtime configuration is documented in
`.env.example`. Original evidence belongs in private mounted storage, never in Git.

# Deterministic demo

All demo records are synthetic and all email is local fixture transport. Say this aloud during
the presentation. Do not describe a fixture reply as a real supplier response.

## Setup

```sh
pixi install --locked
pixi run db-local                 # or: docker compose up -d db
pixi run db-upgrade
pixi run demo-seed
```

`demo-seed` creates a private `.env` if absent, generates an admin token, and copies synthetic
files into `.data/portal`. Read the token locally to sign in; do not paste it into logs or Git.
Existing environments are never overwritten.

For Docker Compose, copy `.env.example` to `.env` first if needed. Set `POSTGRES_PASSWORD`
to a private password and set `COMPOSE_DATABASE_URL` to the matching PostgreSQL SQLAlchemy
URL using host `db`, user/database `chaser`, and port `5432`. URL-encode special password
characters. For host-side migrations and development processes, use the same credentials
in `DATABASE_URL` with host `127.0.0.1`. Compose rejects missing or empty settings. Existing
PostgreSQL volumes retain their original password; changing an environment variable does
not rotate a database role's password.

Two terminals:

```sh
pixi run api
pixi run worker
```

Open http://127.0.0.1:8000, sign in with `ADMIN_TOKEN` from `.env`. The worker polls every 60 seconds.

## Three-minute walkthrough

1. **0:00–0:25.** “Supplier Chaser turns missing or inconsistent commitments into an evidence-backed
   question and tracks the answer.” Show the inbox's explicitly labeled fixture mode.
2. **0:25–0:55.** Open Northstar's quantity case. Show the imported order for 1,200 EA and the
   confirmation for 900 EA; open the source evidence and show the row/page reference.
3. **0:55–1:20.** Show the factual inquiry in the conversation, recipient, sent status and follow-up.
   No payment, contract change or goods release is authorized.
4. **1:20–1:40.** Run `pixi run python -m supplier_chaser.demo amend`. Wait for the next poll.
   The same filename now has a new checksum and an amended claim for 1,000 EA; the original
   quantity case remains and a changed-commitment case explains the revision.
5. **1:40–2:15.** Run `pixi run python -m supplier_chaser.demo reply`. The recorded supplier reply
   explicitly replaces earlier claims and confirms 1,200 EA. The worker validates and reconciles it.
6. **2:15–2:40.** Refresh the case. Confirmation is resolved, physical receipt remains unknown.
   Show structured extraction and the timeline. The Polish supplier's missing-confirmation case
   remains open, demonstrating that the system does not force every case to success.
7. **2:40–3:00.** Refresh after another poll. No duplicate message or case appears. Explain the
   business hypothesis: reduce manual follow-up and expose uncertainty earlier; no measured
   savings are claimed.

For a fast walkthrough set `PORTAL_POLL_SECONDS=5` in the private `.env` before starting worker.
The default remains 60. For a deterministic command-line proof, use `pixi run demo-replay` on a
fresh demo database. It advances an injected clock and checks duplicate-message counts without
sleeping. `pixi run test-freeze` performs the isolated replay without modifying demo state.

## Restart proof

Stop and restart API and worker. Open cases, source versions, thread IDs and follow-up timestamps
remain in PostgreSQL. Restarting does not rerun successful jobs. Never reset the demo database
while the worker is running.

## Deployment

Build `Dockerfile`, provision PostgreSQL 17, attach a private persistent `.data` volume to both
processes, and inject private environment settings. Run migrations once before starting workers.
Run API with `uvicorn supplier_chaser.web.app:create_app --factory --host 0.0.0.0 --port 8000` and
worker with `python -m supplier_chaser.workflow.worker`. Use HTTPS and `COOKIE_SECURE=true`.
Keep an explicit persistent `WEB_SESSION_SECRET` and a strong `ADMIN_TOKEN`.

Smoke check `/health`, authenticated inbox, system heartbeat, a new synthetic file and a fixture
reply after deployment. No public deployment is claimed until a hosting target is configured
and these checks succeed.

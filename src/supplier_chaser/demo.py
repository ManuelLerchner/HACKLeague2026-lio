"""Explicit synthetic demo setup and replay; never used by live adapters."""

import argparse
import json
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from supplier_chaser.adapters.db import (
    CaseRow,
    ClaimRow,
    MessageRow,
    SourceFile,
    make_engine,
    sessions,
)
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.adapters.mail_fixture import FixtureMailGateway
from supplier_chaser.adapters.portal_fixture import FixturePortalClient
from supplier_chaser.clock import FrozenClock
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import Inbound
from supplier_chaser.workflow.worker import Worker

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/demo"


def seed(settings: Settings, fixture_dir: Path = FIXTURES) -> None:
    if settings.portal_mode != "fixture" or settings.mail_mode != "fixture":
        raise ValueError("Demo setup requires fixture portal and mail")
    settings.fixture_portal_dir.mkdir(parents=True, exist_ok=True)
    for name in ("suppliers.json", "orders.csv", "confirmation.json"):
        target = settings.fixture_portal_dir / name
        if not target.exists():
            shutil.copyfile(fixture_dir / name, target)
    settings.fixture_mail_dir.mkdir(parents=True, exist_ok=True)


def deliver_reply(
    settings: Settings, factory: sessionmaker, now: datetime, fixture_dir: Path = FIXTURES
) -> None:
    reply = json.loads((fixture_dir / "reply.json").read_text())
    with factory() as session:
        case = session.scalar(
            select(CaseRow)
            .where(
                CaseRow.po_line_id == f"{reply['po_number']}/{reply['line_number']}",
                CaseRow.issue_type == "QUANTITY_MISMATCH",
            )
            .order_by(CaseRow.created_at)
            .limit(1)
        )
        if case is None:
            raise ValueError("Run initial import first")
        outgoing = session.scalar(
            select(MessageRow)
            .where(MessageRow.case_id == case.id, MessageRow.status == "sent")
            .order_by(MessageRow.sent_or_received_at)
            .limit(1)
        )
        if outgoing is None:
            raise ValueError("No sent inquiry to reply to")
        message = Inbound(
            external_id="<synthetic-reply-1@example.test>",
            sender=outgoing.recipients[0],
            recipients=[outgoing.sender],
            subject="Re: " + outgoing.subject,
            body=reply["evidence_quote"] + "\n\nCONFIRMATION_JSON:" + json.dumps(reply),
            received_at=now,
            in_reply_to=outgoing.external_message_id,
            identity_verified=True,
        )
    inbox = settings.fixture_mail_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "reply-1.json").write_text(message.model_dump_json(indent=2))


def replay(settings: Settings) -> dict:
    seed(settings)
    clock = FrozenClock(datetime(2026, 11, 21, 13, tzinfo=UTC))
    factory = sessions(make_engine(settings.database_url))
    model = FixtureLLM()
    worker = Worker(
        factory,
        settings,
        clock,
        FixturePortalClient(settings.fixture_portal_dir),
        FixtureMailGateway(settings.fixture_mail_dir),
        model,
        model,
    )
    worker.tick()
    clock.value += timedelta(minutes=2)
    shutil.copyfile(FIXTURES / "amendment.json", settings.fixture_portal_dir / "confirmation.json")
    worker.tick()
    clock.value += timedelta(minutes=2)
    deliver_reply(settings, factory, clock.now())
    worker.tick()
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(MessageRow))
    clock.value += timedelta(minutes=2)
    worker.tick()
    with factory() as session:
        after = session.scalar(select(func.count()).select_from(MessageRow))
        assert before == after, "Repeated poll created a duplicate message"
        result = {
            "mode": "synthetic fixture replay",
            "duplicate_messages": after - before,
            "source_versions": session.scalar(select(func.count()).select_from(SourceFile)),
            "claims": session.scalar(select(func.count()).select_from(ClaimRow)),
            "cases": [
                {"po_line": c.po_line_id, "issue": c.issue_type, "state": c.state}
                for c in session.scalars(select(CaseRow).order_by(CaseRow.key))
            ],
        }
        assert any(c["state"] == "RESOLVED" for c in result["cases"])
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "replay", "reply", "amend"])
    action = parser.parse_args().action
    env = Path(".env")
    if not env.exists():
        env.write_text(
            "PORTAL_MODE=fixture\nMAIL_MODE=fixture\nLLM_PROVIDER=fixture\n"
            'TRUSTED_SUPPLIER_PATHS=["suppliers.json"]\n'
            "DRY_RUN=true\nOUTBOUND_MAIL_ENABLED=false\n"
            f"ADMIN_TOKEN={secrets.token_urlsafe(24)}\nWEB_SESSION_SECRET={secrets.token_urlsafe(32)}\n"
        )
        env.chmod(0o600)
    settings = Settings()
    if "suppliers.json" not in settings.trusted_supplier_paths:
        raise ValueError('Demo requires TRUSTED_SUPPLIER_PATHS=["suppliers.json"] in .env')
    if action == "seed":
        seed(settings)
        print("Synthetic fixture portal seeded. Admin token is in .env; start the worker.")
    elif action == "replay":
        print(json.dumps(replay(settings), indent=2))
    elif action == "amend":
        shutil.copyfile(
            FIXTURES / "amendment.json", settings.fixture_portal_dir / "confirmation.json"
        )
        print("Changed synthetic confirmation file; worker detects its new checksum.")
    else:
        deliver_reply(settings, sessions(make_engine(settings.database_url)), datetime.now(UTC))
        print("Recorded synthetic supplier reply placed in fixture mailbox.")


if __name__ == "__main__":
    main()

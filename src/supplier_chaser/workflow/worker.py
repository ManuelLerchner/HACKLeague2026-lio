import logging
import signal
import time
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from supplier_chaser.adapters.db import JobRow, PORow, SourceFact, SystemRow, make_engine, sessions
from supplier_chaser.agent.discover import discover_documents
from supplier_chaser.agent.draft import create_draft
from supplier_chaser.agent.investigate_document import investigate_document
from supplier_chaser.bootstrap import llm_adapter, mail_gateway, portal_client
from supplier_chaser.cases.service import reconcile_line
from supplier_chaser.clock import Clock, SystemClock
from supplier_chaser.communications.receive import interpret_stored_reply, receive
from supplier_chaser.communications.send import send_message
from supplier_chaser.config import Settings
from supplier_chaser.ingestion.mapping import MappingSpec
from supplier_chaser.ingestion.poll import poll_portal
from supplier_chaser.ingestion.review import apply_mapping
from supplier_chaser.ports.llm import LLMDrafter, LLMExtractor
from supplier_chaser.ports.mail import MailGateway
from supplier_chaser.ports.portal import PortalClient
from supplier_chaser.workflow.jobs import claim_job
from supplier_chaser.workflow.scheduler import follow_ups, retry_delay, schedule

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        factory: sessionmaker,
        settings: Settings,
        clock: Clock,
        portal: PortalClient,
        mail: MailGateway,
        extractor: LLMExtractor,
        drafter: LLMDrafter,
    ) -> None:
        self.factory, self.settings, self.clock = factory, settings, clock
        self.portal, self.mail, self.extractor, self.drafter = portal, mail, extractor, drafter
        self.owner = str(uuid4())

    def tick(self) -> None:
        with self.factory.begin() as session:
            schedule(session, self.settings, self.clock.now())
        self.drain()

    def drain(self, limit: int = 100) -> int:
        processed = 0
        while processed < limit:
            with self.factory.begin() as session:
                heartbeat = session.get(SystemRow, "worker")
                if heartbeat:
                    heartbeat.updated_at = self.clock.now()
            job = claim_job(self.factory, self.owner, self.clock.now())
            if job is None:
                break
            try:
                self.execute(job)
                with self.factory.begin() as session:
                    current = session.get(JobRow, job.id)
                    if current and current.lease_owner == self.owner:
                        current.status, current.lease_until = "succeeded", None
            except Exception as exc:
                with self.factory.begin() as session:
                    current = session.get(JobRow, job.id)
                    if current and current.lease_owner == self.owner:
                        permanent = isinstance(exc, (ValueError, TypeError))
                        current.status = (
                            "failed" if permanent or current.attempts >= 5 else "queued"
                        )
                        current.last_error = f"{type(exc).__name__}: job requires inspection"
                        retry_after = (
                            exc.response.headers.get("retry-after")
                            if isinstance(exc, httpx.HTTPStatusError)
                            else None
                        )
                        current.run_after = self.clock.now() + retry_delay(
                            current.attempts, retry_after, self.clock.now()
                        )
                        current.lease_until = None
                logger.warning("Job %s failed (%s)", job.id, type(exc).__name__)
            processed += 1
        return processed

    def execute(self, job: JobRow) -> None:
        now = self.clock.now()
        payload = job.payload_json["payload"]
        if job.job_type == "send":
            send_message(self.factory, payload["message_id"], self.mail, self.settings, now)
            return
        with self.factory.begin() as session:
            if job.job_type == "portal":
                value = poll_portal(session, self.portal, self.settings, now, self.extractor)
                session.merge(SystemRow(key="portal", value=value, updated_at=now))
            elif job.job_type == "mailbox":
                for message in self.mail.receive():
                    receive(session, message, self.extractor, now, self.settings.mail_from)
                session.merge(
                    SystemRow(
                        key="mailbox", value={"last_success": now.isoformat()}, updated_at=now
                    )
                )
            elif job.job_type == "followups":
                follow_ups(session, self.settings, now)
            elif job.job_type == "refresh":
                discover_documents(session, self.settings, now)
                from supplier_chaser.agent.discover import all_document_findings

                findings = (
                    all_document_findings(session)
                    if self.settings.auto_investigate_documents
                    and self.settings.llm_provider != "fixture"
                    else {}
                )
                for line in session.scalars(select(PORow)):
                    reconcile_line(session, line.id, self.settings, now, findings.get(line.id, []))
            elif job.job_type == "reconcile":
                reconcile_line(session, payload["po_line_id"], self.settings, now)
            elif job.job_type == "draft":
                create_draft(
                    session,
                    payload["case_id"],
                    self.drafter,
                    self.settings,
                    now,
                    payload.get("reminder", False),
                    payload.get("require_approval", False),
                )
            elif job.job_type == "review_reply":
                from supplier_chaser.adapters.db import MessageRow

                row = session.get(MessageRow, payload["message_id"])
                review = row.metadata_json.get("buyer_review") if row else None
                if not row or not review or not review["request"]["identity_verified"]:
                    raise ValueError("Reply requires a recorded identity verification")
                interpret_stored_reply(session, row, self.extractor, now)
            elif job.job_type == "apply_mapping":
                table = session.get(SourceFact, payload["table_fact_id"])
                if table is None:
                    raise ValueError("Source table not found")
                apply_mapping(
                    session,
                    table,
                    MappingSpec.model_validate(payload["mapping"]),
                    self.settings,
                    now,
                    self.extractor,
                    payload["approved_by"],
                )
            elif job.job_type == "extract_source":
                from supplier_chaser.agent.discover import relevant_documents

                fact = session.get(SourceFact, payload["source_fact_id"])
                document = next(
                    (
                        item
                        for item in relevant_documents(session)
                        if fact and item[0].id == fact.source_file_id
                    ),
                    None,
                )
                if document:
                    investigate_document(
                        session,
                        document[0].id,
                        document[3],
                        self.settings,
                        self.extractor,
                        self.drafter,
                        now,
                    )
            elif job.job_type == "investigate_document":
                investigate_document(
                    session,
                    payload["source_file_id"],
                    payload["projection_hash"],
                    self.settings,
                    self.extractor,
                    self.drafter,
                    now,
                )
            else:
                raise ValueError("Unknown job type")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings()
    model = llm_adapter(settings)
    worker = Worker(
        sessions(make_engine(settings.database_url)),
        settings,
        SystemClock(),
        portal_client(settings),
        mail_gateway(settings),
        model,
        model,
    )
    running = True

    def stop(signum: int, frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        try:
            worker.tick()
        except Exception as exc:
            logger.warning("Worker tick failed (%s); retrying", type(exc).__name__)
        time.sleep(1)


if __name__ == "__main__":
    main()

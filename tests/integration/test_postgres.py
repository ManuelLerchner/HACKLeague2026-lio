import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from supplier_chaser.adapters.db import Base, CaseRow, JobRow, MessageRow, sessions
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.adapters.mail_fixture import FixtureMailGateway
from supplier_chaser.adapters.portal_fixture import FixturePortalClient
from supplier_chaser.communications.send import send_message
from supplier_chaser.domain.models import SendResult, State
from supplier_chaser.workflow.jobs import claim_job, enqueue
from supplier_chaser.workflow.worker import Worker


@pytest.fixture
def postgres_factory():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL for actual PostgreSQL lock/concurrency verification")
    schema = "test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    try:
        yield sessions(engine)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.postgres
def test_workers_cannot_claim_same_job(postgres_factory, clock):
    with postgres_factory.begin() as session:
        enqueue(session, "draft", {"case_id": "one"}, "concurrent", clock.now())
    barrier = Barrier(2)

    def claim(owner):
        barrier.wait()
        return claim_job(postgres_factory, owner, clock.now())

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ["a", "b"]))
    assert sum(r is not None for r in results) == 1


@pytest.mark.postgres
def test_postgres_loop_and_concurrent_send(postgres_factory, seeded, clock):
    model = FixtureLLM()
    worker = Worker(
        postgres_factory,
        seeded,
        clock,
        FixturePortalClient(seeded.fixture_portal_dir),
        FixtureMailGateway(seeded.fixture_mail_dir),
        model,
        model,
    )
    worker.tick()
    with postgres_factory.begin() as session:
        assert len(list(session.scalars(select(CaseRow)))) == 2
        assert not list(session.scalars(select(JobRow).where(JobRow.status != "succeeded")))
        message = session.scalar(select(MessageRow).where(MessageRow.direction == "outbound"))
        identifier = message.id
        message.status = "queued"
        session.get(CaseRow, message.case_id).state = State.DRAFT_READY

    class Counter:
        calls = 0
        lock = Lock()

        def send(self, message):
            with self.lock:
                self.calls += 1
            return SendResult(status="sent")

    transport = Counter()
    barrier = Barrier(2)

    def send(_):
        barrier.wait()
        send_message(postgres_factory, identifier, transport, seeded, clock.now())

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(send, range(2)))
    assert transport.calls == 1

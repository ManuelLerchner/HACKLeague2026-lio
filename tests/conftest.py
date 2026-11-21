import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from supplier_chaser.adapters.db import Base, sessions
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.adapters.mail_fixture import FixtureMailGateway
from supplier_chaser.adapters.portal_fixture import FixturePortalClient
from supplier_chaser.clock import FrozenClock
from supplier_chaser.config import Settings
from supplier_chaser.workflow.worker import Worker


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/test.db",
        data_dir=tmp_path,
        fixture_portal_dir=tmp_path / "portal",
        fixture_mail_dir=tmp_path / "mail",
        trusted_supplier_paths=["suppliers.json"],
        admin_token="test-secret",
        web_session_secret="test-session-secret",
    )


@pytest.fixture
def factory(settings):
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessions(engine)
    engine.dispose()


@pytest.fixture
def clock():
    return FrozenClock(datetime(2026, 11, 21, 13, tzinfo=UTC))


@pytest.fixture
def worker(settings, factory, clock):
    model = FixtureLLM()
    return Worker(
        factory,
        settings,
        clock,
        FixturePortalClient(settings.fixture_portal_dir),
        FixtureMailGateway(settings.fixture_mail_dir),
        model,
        model,
    )


@pytest.fixture
def seeded(settings):
    settings.fixture_portal_dir.mkdir()
    for name in ("orders.csv", "suppliers.json", "confirmation.json"):
        shutil.copyfile(Path("tests/fixtures/demo") / name, settings.fixture_portal_dir / name)
    return settings

"""Explicitly opted-in connection checks; never send mail or fetch mailbox bodies."""

import hashlib
import imaplib
import os
import smtplib
import ssl
from contextlib import contextmanager

import httpx
import pytest
from openai import OpenAI
from sqlalchemy import create_engine, text

from supplier_chaser.adapters.portal_http import PortalContract, PortalHTTPClient
from supplier_chaser.config import Settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1", reason="Live checks require explicit opt-in"
    ),
]


@contextmanager
def connection_check(service):
    # Provider errors may contain private response bodies or connection strings.
    try:
        yield
    except Exception as error:
        pytest.fail(
            f"{service} check failed ({type(error).__name__}); inspect private configuration",
            pytrace=False,
        )


@pytest.fixture
def live_settings():
    return Settings()


def require(*values):
    if not all(values):
        pytest.fail("Required live connection setting is missing", pytrace=False)


def test_portal(live_settings):
    s = live_settings
    require(s.portal_base_url, s.portal_team_token.get_secret_value())
    contract_json = os.getenv("PORTAL_CONTRACT_JSON")
    with connection_check("Portal"):
        if contract_json:
            contract = PortalContract.model_validate_json(contract_json)
        else:
            require(s.portal_contract_path)
            contract = PortalContract.model_validate_json(s.portal_contract_path.read_text())
        require(s.portal_base_url.startswith("https://"))
        with httpx.Client(
            base_url=s.portal_base_url,
            headers={
                contract.auth_header: contract.auth_prefix + s.portal_team_token.get_secret_value()
            },
            timeout=20,
            follow_redirects=False,
        ) as client:
            response = client.get("/api/v1/openapi.json")
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == contract.openapi_sha256
            files = PortalHTTPClient(client, contract, s.max_file_bytes).list_files()
            assert isinstance(files, list)


def test_smtp(live_settings):
    s = live_settings
    require(s.mail_from, s.smtp_host, s.smtp_user, s.smtp_password.get_secret_value())
    with connection_check("SMTP"):
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as client:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            client.login(s.smtp_user, s.smtp_password.get_secret_value())
            assert client.noop()[0] == 250


def test_imap(live_settings):
    s = live_settings
    require(s.imap_host, s.imap_user, s.imap_password.get_secret_value())
    with connection_check("IMAP"):
        with imaplib.IMAP4_SSL(
            s.imap_host, s.imap_port, ssl_context=ssl.create_default_context(), timeout=20
        ) as client:
            client.login(s.imap_user, s.imap_password.get_secret_value())
            assert client.select("INBOX", readonly=True)[0] == "OK"


@pytest.mark.skipif(os.getenv("CHECK_LLM") != "true", reason="LLM check not selected")
def test_llm_model_access(live_settings):
    s = live_settings
    require(s.llm_api_key.get_secret_value(), s.llm_model)
    with connection_check("LLM model access"):
        with OpenAI(api_key=s.llm_api_key.get_secret_value(), timeout=20, max_retries=0) as client:
            assert client.models.retrieve(s.llm_model).id == s.llm_model


@pytest.mark.skipif(os.getenv("CHECK_DEPLOYMENT") != "true", reason="Deployment check not selected")
def test_deployed_database():
    url = os.getenv("LIVE_DATABASE_URL")
    require(url)
    with connection_check("Deployed PostgreSQL"):
        require(url.startswith("postgresql"))
        engine = create_engine(url, connect_args={"connect_timeout": 20})
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            engine.dispose()


@pytest.mark.skipif(os.getenv("CHECK_DEPLOYMENT") != "true", reason="Deployment check not selected")
def test_deployed_app():
    url = os.getenv("LIVE_APP_URL", "")
    require(url.startswith("https://"))
    with connection_check("Deployed app readiness"):
        response = httpx.get(url.rstrip("/") + "/health", timeout=20, follow_redirects=False)
        response.raise_for_status()
        health = response.json()
        assert health["database"] == "ready"
        assert health["worker_healthy"] is True

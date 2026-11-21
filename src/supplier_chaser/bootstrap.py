import hashlib

import httpx
from openai import OpenAI

from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.adapters.llm_provider import OpenAILLM
from supplier_chaser.adapters.mail_fixture import FixtureMailGateway
from supplier_chaser.adapters.mail_smtp_imap import SMTPIMAPGateway
from supplier_chaser.adapters.portal_fixture import FixturePortalClient
from supplier_chaser.adapters.portal_http import PortalContract, PortalHTTPClient
from supplier_chaser.config import Settings
from supplier_chaser.ports.mail import MailGateway
from supplier_chaser.ports.portal import PortalClient


def portal_client(settings: Settings) -> PortalClient:
    if settings.portal_mode == "fixture":
        return FixturePortalClient(settings.fixture_portal_dir)
    if settings.portal_contract_path is None:
        raise ValueError("Inspect authenticated OpenAPI and supply PORTAL_CONTRACT_PATH first")
    contract = PortalContract.model_validate_json(settings.portal_contract_path.read_text())
    if not settings.portal_base_url.startswith("https://"):
        raise ValueError("Live portal requires HTTPS")
    client = httpx.Client(
        base_url=settings.portal_base_url,
        timeout=20,
        follow_redirects=False,
        headers={
            contract.auth_header: contract.auth_prefix
            + settings.portal_team_token.get_secret_value()
        },
    )
    response = client.get("/api/v1/openapi.json")
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != contract.openapi_sha256:
        raise ValueError("Portal OpenAPI changed; review adapter contract before importing")
    return PortalHTTPClient(client, contract, settings.max_file_bytes)


def mail_gateway(settings: Settings) -> MailGateway:
    if settings.mail_mode == "fixture":
        return FixtureMailGateway(settings.fixture_mail_dir)
    return SMTPIMAPGateway(settings)


def llm_adapter(settings: Settings) -> FixtureLLM | OpenAILLM:
    if settings.llm_provider == "fixture":
        return FixtureLLM()
    return OpenAILLM(
        OpenAI(api_key=settings.llm_api_key.get_secret_value(), timeout=30, max_retries=0),
        settings.llm_model,
    )

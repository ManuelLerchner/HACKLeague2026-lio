import os
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from supplier_chaser.domain.policies import LAUNCH_CUTOFF


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("SUPPLIER_CHASER_ENV_FILE", ".env"), extra="ignore"
    )
    database_url: str = "postgresql+psycopg://chaser:chaser@127.0.0.1:5432/chaser"
    portal_mode: Literal["fixture", "http"] = "fixture"
    portal_base_url: str = ""
    portal_team_token: SecretStr = SecretStr("")
    portal_contract_path: Path | None = None
    portal_poll_seconds: int = Field(default=60, ge=1)
    trusted_supplier_paths: list[str] = []
    max_file_bytes: int = Field(default=10_000_000, gt=0)
    mail_mode: Literal["fixture", "smtp_imap"] = "fixture"
    mail_from: str = "buyer@example.test"
    registered_sender_verified: bool = False
    mailbox_verified: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: SecretStr = SecretStr("")
    # authserv-id of our own receiving server, e.g. "mx.google.com"; empty trusts no header.
    trusted_authserv_id: str = ""
    llm_provider: Literal["fixture", "openai"] = "fixture"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4.1-mini"
    dry_run: bool = True
    outbound_mail_enabled: bool = False
    web_session_secret: SecretStr = SecretStr("")
    admin_token: SecretStr = SecretStr("")
    cookie_secure: bool = False
    timezone: str = "Europe/Berlin"
    data_dir: Path = Path(".data")
    fixture_portal_dir: Path = Path(".data/portal")
    fixture_mail_dir: Path = Path(".data/mail")
    confirmation_grace_hours: int = 24
    launch_cutoff: AwareDatetime = LAUNCH_CUTOFF
    follow_up_hours: int = 4
    max_reminders: int = 1
    max_supplier_sends_per_day: int = 10
    auto_draft: bool = True
    auto_investigate_documents: bool = True

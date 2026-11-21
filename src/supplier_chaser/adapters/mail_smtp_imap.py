import imaplib
import smtplib
import ssl
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from supplier_chaser.config import Settings
from supplier_chaser.domain.models import Inbound, Outbound, SendResult
from supplier_chaser.ingestion.parse import plain_html


def parse_mail(raw: bytes) -> Inbound:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not isinstance(message, EmailMessage):
        raise ValueError("Invalid email")
    senders = getaddresses(message.get_all("From", []))
    if len(senders) != 1 or not message.get("Message-ID"):
        raise ValueError("Message requires a single sender and Message-ID")
    part = message.get_body(preferencelist=("plain", "html"))
    body = str(part.get_content()) if part else ""
    if part and part.get_content_type() == "text/html":
        body = plain_html(body)
    received = parsedate_to_datetime(str(message["Date"])) if message["Date"] else datetime.now(UTC)
    if received.tzinfo is None:
        raise ValueError("Email date has no timezone")
    return Inbound(
        external_id=str(message["Message-ID"]),
        sender=senders[0][1].lower(),
        recipients=[address.lower() for _, address in getaddresses(message.get_all("To", []))],
        subject=str(message.get("Subject", "")),
        body=body,
        received_at=received,
        in_reply_to=str(message["In-Reply-To"]) if message["In-Reply-To"] else None,
        references=str(message.get("References", "")).split(),
    )


class SMTPIMAPGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: Outbound) -> SendResult:
        settings = self.settings
        if settings.dry_run or not settings.outbound_mail_enabled:
            return SendResult(status="dry_run")
        mail = EmailMessage()
        mail["From"], mail["To"] = message.sender, message.recipient
        mail["Subject"], mail["Message-ID"] = message.subject, message.message_id
        mail.set_content(message.body)
        data_started = False
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
                code, _ = smtp.mail(message.sender)
                if code != 250:
                    return SendResult(status="failed_before_send", detail="Sender rejected")
                code, _ = smtp.rcpt(message.recipient)
                if code not in {250, 251}:
                    return SendResult(status="failed_before_send", detail="Recipient rejected")
                data_started = True
                code, _ = smtp.data(mail.as_bytes())
                if code != 250:
                    return SendResult(status="send_uncertain", detail="SMTP DATA not acknowledged")
            return SendResult(status="sent")
        except (OSError, smtplib.SMTPException):
            return SendResult(
                status="send_uncertain" if data_started else "failed_before_send",
                detail="SMTP transport error; inspect provider logs",
            )

    def receive(self) -> list[Inbound]:
        settings = self.settings
        if not settings.mailbox_verified:
            raise ValueError("Verify inbound mailbox before enabling live polling")
        messages = []
        with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=20) as inbox:
            inbox.login(settings.imap_user, settings.imap_password.get_secret_value())
            inbox.select("INBOX", readonly=True)
            status, data = inbox.uid("search", "ALL")
            if status != "OK":
                raise ValueError("Mailbox search failed")
            for identifier in data[0].split():
                status, parts = inbox.uid("fetch", identifier, "(BODY.PEEK[])")
                if status != "OK":
                    raise ValueError("Mailbox fetch failed")
                for part in parts:
                    if isinstance(part, tuple):
                        if len(part[1]) > settings.max_file_bytes:
                            raise ValueError("Email exceeds byte limit")
                        messages.append(parse_mail(part[1]))
        return messages

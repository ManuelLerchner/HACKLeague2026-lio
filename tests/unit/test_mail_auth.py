from email.message import EmailMessage

import pytest

from supplier_chaser.adapters.mail_smtp_imap import parse_mail
from supplier_chaser.communications.mail_auth import authenticate_sender

SENDER = "lin.hui@suppliers.example.test"
GMAIL = (
    "mx.google.com; dkim=pass header.i=@suppliers.example.test header.s=resend; "
    "dkim=pass header.i=@amazonses.com header.s=x; spf=pass (google.com: domain of "
    "bounce@rsend.suppliers.example.test designates 54.240.6.244 as permitted sender) "
    "smtp.mailfrom=bounce@rsend.suppliers.example.test"
)


def mail(*results: str) -> EmailMessage:
    message = EmailMessage()
    for result in results:
        message["Authentication-Results"] = result
    message["From"] = f"Lin Hui <{SENDER}>"
    message["To"] = "buyer@example.test"
    message["Subject"] = "Re: [SC:x] confirmation"
    message["Message-ID"] = "<reply@example.test>"
    message["In-Reply-To"] = "<question@example.test>"
    message["Date"] = "Sat, 21 Nov 2026 14:00:00 +0100"
    message.set_content("Confirmed.")
    return message


def test_aligned_dkim_from_trusted_receiver_authenticates():
    assert authenticate_sender(mail(GMAIL), SENDER, "mx.google.com") is None
    dmarc = "mx.google.com; dmarc=pass (p=NONE) header.from=suppliers.example.test"
    assert authenticate_sender(mail(dmarc), SENDER, "mx.google.com") is None


@pytest.mark.parametrize(
    ("results", "trusted", "reason"),
    [
        ([GMAIL], "", "No trusted"),
        ([], "mx.google.com", "no Authentication-Results"),
        ([GMAIL.replace("mx.google.com;", "mx.evil.test;")], "mx.google.com", "not from"),
        # Only the relay's own signature passes; it says nothing about the From domain.
        (["mx.google.com; dkim=pass header.i=@amazonses.com"], "mx.google.com", "aligned"),
        (["mx.google.com; dkim=fail header.i=@suppliers.example.test"], "mx.google.com", "aligned"),
        (["mx.google.com; dkim=pass header.i=@example.test"], "mx.google.com", "aligned"),
        # A sender-written header below the receiver's topmost one is ignored.
        (["mx.google.com; dkim=none", GMAIL], "mx.google.com", "aligned"),
        (["mx.google.com; dkim="], "mx.google.com", "aligned"),
    ],
)
def test_untrusted_or_unaligned_results_fail(results, trusted, reason):
    failure = authenticate_sender(mail(*results), SENDER, trusted)
    assert failure is not None and reason in failure


def test_parse_mail_records_authentication():
    raw = mail(GMAIL).as_bytes()
    assert parse_mail(raw, "mx.google.com").identity_verified
    unverified = parse_mail(raw)
    assert not unverified.identity_verified
    assert "No trusted" in unverified.authentication

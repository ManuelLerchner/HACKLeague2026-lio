"""Sender authentication from the receiving server's Authentication-Results header."""

import re
from email.message import EmailMessage


def _strip_comments(value: str) -> str:
    return re.sub(r"\([^()]*\)", " ", value)


def _domain(value: str) -> str:
    return value.rsplit("@", 1)[-1].strip().strip(".").lower()


def authenticate_sender(message: EmailMessage, sender: str, trusted_authserv_id: str) -> str | None:
    """Return None when authenticated, else the reason authentication failed.

    Only the topmost header counts: the receiving server prepends its own, so any
    header below it may have been written by the sender. Alignment is strict, so a
    signature from another domain (e.g. the sending relay) never vouches for From.
    """
    if not trusted_authserv_id:
        return "No trusted receiving mail server is configured"
    headers = message.get_all("Authentication-Results") or []
    if not headers:
        return "Receiving server added no Authentication-Results"
    authserv_id, _, results = _strip_comments(str(headers[0])).partition(";")
    if authserv_id.split()[:1] != [trusted_authserv_id]:
        return "Authentication-Results is not from the trusted receiving server"
    from_domain = _domain(sender)
    for result in results.split(";"):
        method, _, rest = result.strip().partition("=")
        verdict, *properties = rest.split() or [""]
        if verdict.lower() != "pass" or method.lower() not in {"dkim", "dmarc"}:
            continue
        values = dict(p.split("=", 1) for p in properties if "=" in p)
        if method.lower() == "dmarc":
            domains = {values.get("header.from", "")}
        else:
            domains = {values.get("header.d", ""), values.get("header.i", "")}
        if from_domain in {_domain(d) for d in domains if d}:
            return None
    return f"No DKIM or DMARC pass aligned with {from_domain}"

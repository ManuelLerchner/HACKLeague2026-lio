from email.message import EmailMessage

import httpx
import pytest
import respx
from pydantic import ValidationError

from supplier_chaser.adapters.mail_smtp_imap import parse_mail
from supplier_chaser.adapters.portal_fixture import FixturePortalClient, validate_path
from supplier_chaser.adapters.portal_http import PortalContract, PortalHTTPClient
from supplier_chaser.communications.correlation import correlate
from supplier_chaser.domain.models import Confirmation, Draft, Inbound
from supplier_chaser.ingestion.normalize import normalize_po


def portal(client):
    return PortalHTTPClient(
        client,
        PortalContract(
            openapi_sha256="test",
            auth_header="X-Test",
            path_key="path",
            checksum_key="sha256",
            size_key="size",
            media_type_key="type",
            list_key="files",
        ),
        100,
    )


@respx.mock
@pytest.mark.parametrize("status", [401, 429, 500])
def test_portal_failures_propagate(status):
    respx.get("https://portal.example.test/api/v1/files").mock(return_value=httpx.Response(status))
    with httpx.Client(base_url="https://portal.example.test") as client:
        with pytest.raises(httpx.HTTPStatusError):
            portal(client).list_files()


@respx.mock
def test_portal_metadata_and_missing_keys():
    route = respx.get("https://portal.example.test/api/v1/files")
    route.mock(
        return_value=httpx.Response(
            200,
            json={"files": [{"path": "a.csv", "sha256": "abc", "size": 12, "type": "text/csv"}]},
        )
    )
    with httpx.Client(base_url="https://portal.example.test") as client:
        assert portal(client).list_files()[0].path == "a.csv"
        route.mock(return_value=httpx.Response(200, json={"files": [{}]}))
        with pytest.raises(KeyError):
            portal(client).list_files()


@respx.mock
def test_portal_timeout():
    respx.get("https://portal.example.test/api/v1/files").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with httpx.Client(base_url="https://portal.example.test") as client:
        with pytest.raises(httpx.ReadTimeout):
            portal(client).list_files()


@respx.mock
def test_reviewed_listing_without_mime_ignores_returned_url():
    respx.get("https://portal.example.test/api/v1/files").respond(
        200,
        json={
            "count": 1,
            "files": [
                {
                    "path": "orders.csv",
                    "sha256": "abc",
                    "bytes": 4,
                    "url": "https://untrusted.example.test/collect",
                }
            ],
        },
    )
    download = respx.get("https://portal.example.test/api/v1/files/orders.csv").respond(
        200, content=b"test", headers={"content-type": "application/octet-stream"}
    )
    with httpx.Client(base_url="https://portal.example.test") as client:
        adapter = PortalHTTPClient(
            client,
            PortalContract(
                openapi_sha256="test",
                auth_header="Authorization",
                auth_prefix="Bearer ",
                list_key="files",
                path_key="path",
                checksum_key="sha256",
                size_key="bytes",
                complete_listing=True,
            ),
            max_bytes=4,
        )
        metadata = adapter.list_files()[0]
        assert metadata.media_type == "text/csv"
        assert adapter.complete_listing
        assert adapter.download(metadata) == b"test"
        assert download.called


def test_fixture_protocol(seeded):
    portal = FixturePortalClient(seeded.fixture_portal_dir)
    listing = portal.list_files()
    assert len(listing) == 3
    assert all(len(portal.download(f)) == f.size for f in listing)


@pytest.mark.parametrize(
    "path", ["../secrets", "/etc/passwd", "a/../../secret", "a\\secret", "a//b"]
)
def test_path_traversal(path):
    with pytest.raises(ValueError):
        validate_path(path)


@pytest.mark.parametrize("body", ["Potwierdzamy dostawę.", "배송일 확인", "确认交货", "Confirmed."])
def test_multilingual_multipart_mail(body):
    message = EmailMessage()
    message["From"] = "Łukasz <supplier@example.test>"
    message["To"] = "buyer@example.test"
    message["Subject"] = "Re: confirmation"
    message["Message-ID"] = "<reply@example.test>"
    message["In-Reply-To"] = "<question@example.test>"
    message["References"] = "<older@example.test> <question@example.test>"
    message["Date"] = "Sat, 21 Nov 2026 14:00:00 +0100"
    message.set_content(body)
    message.add_alternative(f"<p>{body}</p>", subtype="html")
    parsed = parse_mail(message.as_bytes())
    assert parsed.body.strip() == body
    assert parsed.in_reply_to == "<question@example.test>"
    assert parsed.sender == "supplier@example.test"
    assert not parsed.identity_verified


def test_schemas_reject_unauthorized_actions_and_invalid_values():
    with pytest.raises(ValidationError):
        Draft(subject="x", body="accept fee", language="en", action="accept_terms")
    with pytest.raises(ValidationError):
        Confirmation(
            document_id="x",
            po_number="1",
            line_number="1",
            supplier_id="S",
            unit="EA",
            deliveries=[{"quantity": "unknown"}],
        )


def test_sap_legacy_keeps_leading_zero_ids():
    po = normalize_po(
        {
            "EBELN": "00123",
            "EBELP": "00010",
            "LIFNR": "0001",
            "MATNR": "X",
            "MENGE": "5",
            "MEINS": "EA",
            "EINDT": "2026-11-22",
            "CREATED_AT": "2026-11-19T00:00:00Z",
        }
    )
    assert po.po_number == "00123" and po.line_number == "00010"


def test_ambiguous_correlation_requires_review(clock):
    message = Inbound(
        external_id="x",
        sender="a@example.test",
        recipients=["buyer@example.test"],
        subject="Re",
        body="PO P1",
        received_at=clock.now(),
        references=["m1", "m2"],
    )
    assert (
        correlate(
            message, [("m1", "c1", "a@example.test", "P1"), ("m2", "c2", "a@example.test", "P1")]
        )
        is None
    )

# Assumptions and verification boundaries

The three supplied challenge PDFs were read locally. None is copied into this repository.
All committed supplier, order, confirmation and mailbox fixtures are invented `.example.test`
records. They demonstrate behavior, not findings about the confidential portal data.

## Confirmed from challenge documents

- `Lio_Challenge_Keep_the_Line_Running.pdf`, pp. 5–6: files arrive without announcement;
  file list, individual download, archive and OpenAPI routes exist. Team-token authentication
  is required, but the header name and response schema are not specified in these documents.
- Registered sender addresses are required for supplier replies. No inbound mailbox API is promised.
- Page 8 and `Lio_Challenge_Judging.pdf`, p. 2: Sunday 12:00 submission;
  demos start Sunday 14:00. The judging document specifies three minutes of pitch plus two
  minutes of questions. These are document-confirmed times, not a check of later organizer updates.
- Launch cutoff is Sunday 22 November 2026, 20:00 Europe/Berlin.

## Integration assumptions

| Area | Implemented boundary | Verification still required |
| --- | --- | --- |
| Portal | Authenticated OpenAPI/list/download verified; Bearer auth and SHA-256 checked; fixture and contract-configured HTTP clients | Genuine business-record mapping and ongoing schema compatibility |
| SAP | Two explicit synthetic formats: canonical named columns and SAP-style column aliases | Both genuine exports, timestamp/version semantics and unit mappings |
| Contacts | Only configured trusted supplier-metadata paths establish addresses | Confirm genuine master-data path and each outbound recipient |
| Confirmation | Structured JSON or extracted text with a quoted span | Genuine documents, scans, split and amended confirmation conventions |
| Mail | Fixture adapter; Gmail SMTP/IMAP and one threaded challenge-supplier exchange verified | Automated trusted sender-authentication signals; registration is not exposed by the portal API |
| LLM | Deterministic recorded extraction; OpenAI structured-output adapter | Provider credentials, model availability, multilingual extraction accuracy |
| PP-07 | All costly/contractual/payment/release proposals require review | Actual policy passages and authorized approvers; no replacement for a vacant role inferred |
| Deployment | Container and separate API/worker commands | Hosting target, managed PostgreSQL, secret configuration, persistent blob volume, public smoke test |

Live IMAP messages deliberately have `identity_verified=false`: matching a From address is
insufficient to authenticate a sender. Until a trusted provider authentication contract is wired,
they are preserved and escalated, not allowed to resolve a case automatically.

The manual live exchange asked only for an existing order's quantity/date. The reply included
release and shipping-term conditions, which require buyer review. Nothing was accepted or
released. Its raw messages and audit remain private under `.data/`; the demo database still
contains only synthetic cases. Automatic external sending remains disabled.

The authenticated portal list has `count` and `files`; each file has `path`, `bytes`, `sha256`
and `url`, with no MIME field. MIME is inferred from the path and checked on download. Private
contracts and downloaded records stay under `.data/`. The genuine current PO export has no unit
column; mapping it requires trusted unit evidence, not an invented default.

Fixture extraction requires an explicit recorded `CONFIRMATION_JSON:` payload. It does not claim
to understand arbitrary natural-language email. Live model drafts require buyer approval; only
the bounded deterministic question templates may enter the automatic send path.

## Scope limits

- Initial import enumerates all files through the same incremental path. Archive ZIP optimization
  is deferred until its genuine content and path conventions can be checked.
- No OCR, shipment/receipt projection or launch-workbook ingestion is claimed. Unrecognized
  workbook columns and scanned PDFs become visible review items. Missing receipt stays unknown.
- Missing dates, incompatible units and uncertain intent remain unresolved. Dates without times
  are not converted to invented deadlines. The configured launch cutoff affects priority only
  when launch relevance and order dates are known.
- Free-text shipping/contract terms require policy review in this MVP. Extracted quantities and
  ISO dates must appear in the cited span; locale-specific date text that cannot satisfy this
  conservative evidence check becomes a review item rather than an automatically accepted claim.
- A NO_RESPONSE event belongs to the original case. One reminder is allowed, then escalation;
  the app does not create a second supplier inquiry merely to label a no-response case.
- Live IMAP currently scans the mailbox and deduplicates in the database. Use a dedicated,
  small mailbox. Incremental UID checkpoints and attachment extraction are future work.
- Evidence bodies are private database text; original files are content-addressed private blobs.
  Production storage encryption and backup configuration belong to the deployment environment.
- No ERP writes, contracting, payments, booking, goods release or inferred physical delivery.

## Dependency ownership

`pixi.toml` is authoritative for runtime and developer dependencies. `pyproject.toml` contains
package metadata and tool configuration, without duplicating dependencies. `pixi.lock` pins
both Linux x86_64 and Apple Silicon. Python is pinned to the 3.12 minor series.

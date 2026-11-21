# Integration contracts

## Portal setup

1. Read the authenticated `/api/v1/openapi.json` using the organizer-documented authentication.
   Store the file privately under `.data/`, never in Git. Do not guess the header name.
2. Map the actual list schema into a private `PortalContract` JSON file. Fields:
   `openapi_sha256`, `auth_header`, `auth_prefix`, `list_key` (null for a top-level array),
   `path_key`, `checksum_key`, `size_key`, `media_type_key`, `checksum_algorithm`, `complete_listing`.
3. Set `PORTAL_MODE=http`, `PORTAL_BASE_URL`, `PORTAL_TEAM_TOKEN`, `PORTAL_CONTRACT_PATH`.
   Startup retrieves OpenAPI and checks its SHA-256 against the reviewed contract.
4. Set `TRUSTED_SUPPLIER_PATHS` to reviewed master-data paths. Arbitrary confirmation documents
   cannot establish or change a supplier email address.
5. Adapt the deterministic parser to genuine schemas; add sanitized contract fixtures first.

Downloads never use a returned arbitrary URL. Paths are validated and encoded below the configured
portal origin. Raw files are stored by SHA-256, never by a remote filename. Maximum bytes and listed
size are enforced; SHA-256 checksums are verified when their representation is recognized.
Opaque checksums still identify source versions but cannot prove integrity.

## Synthetic canonical records

JSON uses `{"records": [...]}`. Every record has `kind`: `supplier`, `po`, or `confirmation`.
The Pydantic models in `domain/models.py` define the fields and reject unknown keys.
CSV has a `kind` column; legacy SAP-style CSV uses `EBELN`, `EBELP`, `LIFNR`, `MATNR`,
`MENGE`, `MEINS`, `EINDT`, `CREATED_AT`. IDs remain strings, including leading zeros.

PO updates require an increased `document_version`; equal-version conflicts require review.
Confirmation precedence requires explicit `supersedes` document identifiers. Independent conflicting
claims remain contradictory even if one arrived later. Split deliveries sum within one confirmation;
repeated confirmations do not add quantities. No implicit unit conversion occurs.

## Mail setup

Use a dedicated inbox. Set SMTP/IMAP hosts, ports and credentials in `.env` or deployment secrets.
SMTP requires STARTTLS; IMAP requires TLS. Verify sender registration in the portal, then set
`REGISTERED_SENDER_VERIFIED=true`. Verify actual inbound delivery before setting
`MAILBOX_VERIFIED=true`. Configure `OUTBOUND_ALLOWLIST` as a JSON array of exact addresses.

Keep `DRY_RUN=true` and `OUTBOUND_MAIL_ENABLED=false` through setup. Test with team-controlled
addresses before changing these flags. Fixture transport never sends external email, regardless
of the live-send flags. Its UI is explicitly labeled.

The database stores the draft and send intent before transport. An interrupted `sending` record
becomes `send_uncertain`; no automatic retry is permitted. Reconcile with provider Sent records
or logs manually. Failed-before-send records also require a buyer to prepare a new attempt.

Inbound deduplication uses Message-ID plus sender/body content hash. Correlation combines thread
references, stable case marker, known recipient and PO identity. Ambiguity and identity uncertainty
are review items. Supplier text cannot select a recipient or grant spending authority.

## LLM setup

Set `LLM_PROVIDER=openai`, `LLM_API_KEY`, and `LLM_MODEL` only after verifying access.
The SDK's structured response is validated by Pydantic. A verbatim evidence quote is required;
extraction receives at most two validation attempts. Provider network failures use durable job
backoff. Secret settings are never passed to the model.

The model has no shell, browser, database session or action tools. Application use cases own
draft creation, policy checks and sending. Human review remains required for model-generated
drafts and unverifiable live-mail identity. Prompt and model versions accompany messages.

## Worker and storage

Domain and ports are independent of SQLAlchemy, HTTP and provider SDKs. The composition root is
`bootstrap.py`; use cases accept protocols or explicit transaction sessions. PostgreSQL jobs use
row locks with `SKIP LOCKED`, lease ownership and expiry. Successful jobs cannot be reclaimed.
Database constraints protect source versions, inbound messages and active logical case keys.

The API does not run background work. It commits jobs and returns HTTP 202 for queued operations.
API and worker must point to the same PostgreSQL database and private blob volume.

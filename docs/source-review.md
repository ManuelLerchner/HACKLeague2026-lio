# Reviewing unfamiliar sources

The importer separates structure discovery from approved business interpretation. A CSV,
flat JSON table or worksheet with unfamiliar columns is stored as a reviewable table.
PDF/text content is preserved even when extraction fails. Original bytes, checksum and row/page
references remain private evidence; column-name suggestions never create business records.

Open **Source review**, choose a file and select whether the table contains purchase orders,
supplier contacts or confirmations. Map source columns, provide only evidenced fixed values,
and select date/number formats explicitly. Optional row filters restrict the import to relevant
records. Preview validates every selected row before approval queues a durable import job.
Only an explicitly trusted supplier mapping can establish recipient addresses.

Approved mappings are scoped to the exact source path, table locator and column fingerprint.
Reordered columns are supported. Added/removed columns require review. Repeated polls and
repeated approval have one logical effect. Conflicting PO versions and changed supplier
addresses return to review instead of overwriting established evidence. A mapped version cannot
be silently remapped in place; corrected source versions require a new reviewed import.

Calendar-only order dates stay dates. Missing units stay unknown. A fixed unit is appropriate
only after checking the material master for all selected rows; put that provenance in the review
note. Importing ERP status or a promised date does not establish a supplier commitment or receipt.

Worksheets may have title/group-heading rows: discovery proposes the first complete unique
header row and preserves the preceding text. Review the proposal against the original file.
The current limits are 10,000 rows, 200 columns and 50 MB expanded workbook data. Nested JSON,
scanned images, DOCX and layouts without usable text/table structure remain manual review items.

## AI extraction

With a model configured, `AUTO_INVESTIGATE_DOCUMENTS=true` schedules whole-document
investigation when text identifies an imported PO. All candidate order lines are considered
together. Item numbers are not silently mapped to PO line numbers; ambiguous attribution
requires clarification. Missing units remain unknown. Approved table mappings still require review.

Quantities and dates must be supported by quoted source text. Conditions and risks remain
visible and are never accepted automatically. Investigation may prepare a factual draft for
buyer approval; it does not send it. `AUTO_DRAFT=false` does not disable document investigation.
Set `AUTO_INVESTIGATE_DOCUMENTS=false` to stop automatic document model calls.

Use **Preview original evidence** to open an in-app preview. Downloads remain available
explicitly. Source and case pages update after worker actions while preserving unsaved edits.
The **Worker queue** shows real queued/running/failed jobs; missing analysis alone is not a
queued job. **What happens next** on a case explains the next buyer action and whether waiting
will help. Escalated drafts must be replaced before they can be approved for sending.

## Separate local live workspace

Create a private `.env.live` with a separate PostgreSQL database and `DATA_DIR`. Keep all
credentials out of Git. `SUPPLIER_CHASER_ENV_FILE` selects the configuration at process startup:

```sh
SUPPLIER_CHASER_ENV_FILE=.env.live pixi run db-upgrade
SUPPLIER_CHASER_ENV_FILE=.env.live pixi run uvicorn supplier_chaser.web.app:create_app --factory --host 127.0.0.1 --port 8001
SUPPLIER_CHASER_ENV_FILE=.env.live pixi run worker
```

Set `PORTAL_MODE=http`, a reviewed portal contract, `MAIL_MODE=smtp_imap`, and
`LLM_PROVIDER=openai` only after their connection checks pass. Use `AUTO_DRAFT=false` during
bulk import and set `AUTO_INVESTIGATE_DOCUMENTS=false` if model calls must wait for manual review.
Changing `.env.live` requires restarting the API and worker.

Start with `DRY_RUN=true` and `OUTBOUND_MAIL_ENABLED=false`. For an authorized live test, configure
verified supplier contacts, a registered sender and verified mailbox, then enable
sending and approve one factual draft. Send caps include previously recorded sent messages.
Live IMAP messages remain subject to identity review; the current adapter does not automatically
trust From headers or resolve cases from a real-mail promise.

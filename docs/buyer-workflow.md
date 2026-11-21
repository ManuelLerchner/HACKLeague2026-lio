# Buyer workflow

The inbox groups cases by **Needs your action**, **In progress**, **Waiting for supplier**,
then **Completed**. Priority orders cases within each group. Each row names its next action.

1. Open a case under **Needs your action**. Check the PO, line and existing source files.
   “Missing confirmation” means no applicable imported claim; an unprocessed file may contain one.
2. Choose **Draft supplier question**. This queues work and always requires send approval,
   including in fixture mode. Refresh to see the draft or an explicit failed-job notice.
3. Review/edit the draft, then **Approve factual inquiry & queue send**. Existing recipient
   supplier-contact checks, sender verification, dry-run settings and send caps still apply.
4. Wait for the reply. A reply without transport-verified identity appears under **Needs your action**.
5. In **Review supplier reply**, enter your name and verification evidence. Verify the sender and
   order attribution against trusted records, check the attestation, then **Verify identity & process
   reply**. Alternatively reject the reply with a reason. Reviews are authenticated, CSRF-protected,
   audited and idempotent. An identity review never approves fees, freight, releases or revised terms.
6. The worker extracts the original message, validates provenance and reruns reconciliation. A matching
   unconditional claim resolves the confirmation discrepancy. Conditions, ambiguous extraction or
   remaining discrepancies stay escalated with the reply and extracted evidence visible.
7. A buyer can request **Draft factual clarification** after reviewing a held reply. Send caps still
   apply; this does not bypass policy. Commercial decisions remain outside this MVP.

Successful reply processing does not resend email. Rejected replies create no commitment. Periodic
reconciliation cannot close a case while a reply is awaiting review or extraction. Unsupported date
formats remain review items; ISO dates and explicit English day-month-name-year dates are validated.

Restart the API and worker after updating Python code. Both must use the same environment file and
database. In a live workspace, keep the existing send caps and trusted supplier metadata; tests use fixture transport
and do not authorize additional live emails.

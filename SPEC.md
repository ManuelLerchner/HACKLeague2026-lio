# Supplier Chaser implementation contract

This repository implements the supplied “Supplier Chaser — Implementation Specification v1.0”
for the Lio START Hack Munich 2026 challenge. The user-supplied specification is the governing
scope; this file summarizes its acceptance contract without copying confidential source documents.

The product reads changing supplier/PO evidence, detects confirmation discrepancies, maintains
one active logical case per supplier/order line/issue, asks a bounded factual question, processes
a correlated supplier reply, and produces an evidence-backed next action. Repeated files and
messages have one logical effect. Uncertainty, policy-sensitive proposals and sender-authentication
failures remain unresolved and require review.

Runtime: one FastAPI HTTP/UI process, one background worker, shared PostgreSQL and private blob
storage. No Redis, Celery, broker, microservices, ERP writes or physical-operation claims.

Acceptance checks cover deterministic rules, typed adapter contracts, frozen clocks, golden replay,
durable jobs and restart behavior, sender/recipient restrictions, uncertain-send recovery,
authentication, private evidence, and the complete synthetic demonstration.

External readiness additionally requires authenticated portal schema inspection, actual supplier
formats, registered/verified mailbox, a configured hosting target, deployment smoke checks and
organizer submission. `docs/assumptions.md` records precisely what remains unverified.

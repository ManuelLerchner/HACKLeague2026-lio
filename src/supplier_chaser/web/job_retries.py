from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from supplier_chaser.adapters.db import CaseRow, JobRow, MessageRow
from supplier_chaser.clock import Clock
from supplier_chaser.workflow.retry import (
    retry_job,
    retry_rate_limited_message,
    retry_reply_extraction,
)


def register_retry_routes(
    app: FastAPI, factory: sessionmaker, clock: Clock, authorized: Callable
) -> None:
    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    def retry_processing(job_id: str, actor: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            job = session.scalar(select(JobRow).where(JobRow.id == job_id).with_for_update())
            if job is None:
                raise HTTPException(404, "Job not found")
            try:
                retry_job(session, job, actor, clock.now())
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/messages/{message_id}/retry-send", status_code=202)
    def retry_limited_send(message_id: str, actor: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            case_id = session.scalar(select(MessageRow.case_id).where(MessageRow.id == message_id))
            if case_id is not None:
                session.scalar(select(CaseRow).where(CaseRow.id == case_id).with_for_update())
            message = session.scalar(
                select(MessageRow).where(MessageRow.id == message_id).with_for_update()
            )
            if message is None:
                raise HTTPException(404, "Message not found")
            try:
                job = retry_rate_limited_message(session, message, actor, clock.now())
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/messages/{message_id}/retry-extraction", status_code=202)
    def retry_extraction(message_id: str, actor: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            message = session.scalar(
                select(MessageRow).where(MessageRow.id == message_id).with_for_update()
            )
            if message is None:
                raise HTTPException(404, "Message not found")
            try:
                job = retry_reply_extraction(session, message, actor, clock.now())
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {"job_id": job.id, "status": job.status}

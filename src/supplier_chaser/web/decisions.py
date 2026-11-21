from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import sessionmaker

from supplier_chaser.cases.decisions import DecisionRequest, record_decision
from supplier_chaser.clock import Clock


def register_decision_routes(
    app: FastAPI, factory: sessionmaker, clock: Clock, authorized: Callable
) -> None:
    @app.post("/api/v1/decisions", status_code=202)
    def decide(body: DecisionRequest, actor: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            try:
                row, lines = record_decision(session, body, clock.now())
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return {"decision_id": row.id, "rechecking_lines": len(lines)}

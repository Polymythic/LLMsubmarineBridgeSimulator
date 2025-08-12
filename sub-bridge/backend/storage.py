from __future__ import annotations
import datetime as dt
from typing import Optional, Any

# Provide a graceful fallback when sqlmodel is unavailable (e.g., in minimal test envs)
try:  # pragma: no cover - import guard behavior not core logic
    from sqlmodel import Field, SQLModel, create_engine, Session  # type: ignore
    _HAVE_SQLMODEL = True
except Exception:  # pragma: no cover
    Field = object  # type: ignore
    SQLModel = object  # type: ignore
    Session = object  # type: ignore
    create_engine = lambda *a, **k: None  # type: ignore
    _HAVE_SQLMODEL = False


if _HAVE_SQLMODEL:
    class Run(SQLModel, table=True):  # type: ignore[misc]
        id: Optional[int] = Field(default=None, primary_key=True)  # type: ignore[call-arg]
        started_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow())  # type: ignore[call-arg]


    class Snapshot(SQLModel, table=True):  # type: ignore[misc]
        id: Optional[int] = Field(default=None, primary_key=True)  # type: ignore[call-arg]
        run_id: int = Field(index=True)  # type: ignore[call-arg]
        created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow(), index=True)  # type: ignore[call-arg]
        ownship_heading: float
        ownship_speed: float
        ownship_depth: float


    class Event(SQLModel, table=True):  # type: ignore[misc]
        id: Optional[int] = Field(default=None, primary_key=True)  # type: ignore[call-arg]
        run_id: int = Field(index=True)  # type: ignore[call-arg]
        created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow(), index=True)  # type: ignore[call-arg]
        type: str
        payload: str


def init_engine(sqlite_path: str):
    if not _HAVE_SQLMODEL:
        return None
    engine = create_engine(f"sqlite:///{sqlite_path}", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


def create_run(engine) -> int:
    if not _HAVE_SQLMODEL or engine is None:
        return 0
    with Session(engine) as session:  # type: ignore[call-arg]
        run = Run()  # type: ignore[operator]
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id  # type: ignore[return-value]


def insert_snapshot(engine, run_id: int, heading: float, speed: float, depth: float) -> None:
    if not _HAVE_SQLMODEL or engine is None:
        return
    with Session(engine) as session:  # type: ignore[call-arg]
        snap = Snapshot(run_id=run_id, ownship_heading=heading, ownship_speed=speed, ownship_depth=depth)  # type: ignore[operator]
        session.add(snap)
        session.commit()


def insert_event(engine, run_id: int, type_: str, payload: str) -> None:
    if not _HAVE_SQLMODEL or engine is None:
        return
    with Session(engine) as session:  # type: ignore[call-arg]
        ev = Event(run_id=run_id, type=type_, payload=payload)  # type: ignore[operator]
        session.add(ev)
        session.commit()

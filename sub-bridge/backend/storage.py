from __future__ import annotations
import datetime as dt
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow())


class Snapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow(), index=True)
    ownship_heading: float
    ownship_speed: float
    ownship_depth: float


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow(), index=True)
    type: str
    payload: str


def init_engine(sqlite_path: str):
    engine = create_engine(f"sqlite:///{sqlite_path}", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


def create_run(engine) -> int:
    with Session(engine) as session:
        run = Run()
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id  # type: ignore[return-value]


def insert_snapshot(engine, run_id: int, heading: float, speed: float, depth: float) -> None:
    with Session(engine) as session:
        snap = Snapshot(run_id=run_id, ownship_heading=heading, ownship_speed=speed, ownship_depth=depth)
        session.add(snap)
        session.commit()


def insert_event(engine, run_id: int, type_: str, payload: str) -> None:
    with Session(engine) as session:
        ev = Event(run_id=run_id, type=type_, payload=payload)
        session.add(ev)
        session.commit()

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, path: str):
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 10})

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Session]:
        with self.engine.connect() as connection:
            # Start before the first read so capacity and stock checks share a snapshot.
            connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            with Session(bind=connection, join_transaction_mode="control_fully") as session:
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

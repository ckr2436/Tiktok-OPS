from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.data.db import _has_writes, _reset_mutation_flag


def test_first_statement_raw_dml_keeps_write_marker():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with Session(engine, future=True) as session:
        session.execute(text("create table write_probe (id integer primary key)"))
        session.commit()
        _reset_mutation_flag(session)

        session.execute(text("insert into write_probe (id) values (1)"))

        assert _has_writes(session) is True


def test_nested_raw_dml_keeps_write_marker_until_root_transaction_ends():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with Session(engine, future=True) as session:
        session.execute(text("create table write_probe (id integer primary key)"))
        session.commit()
        session.execute(text("select 1"))
        _reset_mutation_flag(session)

        with session.begin_nested():
            session.execute(text("insert into write_probe (id) values (1)"))

        assert _has_writes(session) is True
        session.commit()
        assert _has_writes(session) is False

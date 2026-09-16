from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import MetaData, UniqueConstraint, create_engine, event, select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Base, Security, SecuritySignalSnapshot
from trade_news_analysis.services.opportunities import latest_snapshot_map


def test_latest_snapshot_query_loads_only_latest_per_security(session: Session) -> None:
    securities = list(
        session.scalars(
            select(Security).where(Security.symbol.in_(["AAPL", "MSFT"])).order_by(Security.id)
        )
    )
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    expected: dict[int, int] = {}
    for security in securities:
        newest = SecuritySignalSnapshot(security_id=security.id, horizon=5, as_of=now)
        session.add(newest)
        session.flush()
        expected[security.id] = newest.id
        session.add_all(
            [
                SecuritySignalSnapshot(
                    security_id=security.id,
                    horizon=5,
                    as_of=now - timedelta(days=days),
                )
                for days in range(1, 21)
            ]
        )
        session.add(
            SecuritySignalSnapshot(
                security_id=security.id, horizon=1, as_of=now + timedelta(days=1)
            )
        )
    session.commit()
    session.expunge_all()
    loaded_ids: list[int] = []

    def track_loaded_snapshot(_session: Session, instance: object) -> None:
        if isinstance(instance, SecuritySignalSnapshot):
            loaded_ids.append(instance.id)

    event.listen(session, "loaded_as_persistent", track_loaded_snapshot)
    try:
        snapshots = latest_snapshot_map(session, 5)
    finally:
        event.remove(session, "loaded_as_persistent", track_loaded_snapshot)
    assert {security_id: snapshot.id for security_id, snapshot in snapshots.items()} == expected
    assert sorted(loaded_ids) == sorted(expected.values())
    assert latest_snapshot_map(session, 20) == {}
    assert latest_snapshot_map(session, 5, set()) == {}
    selected_id = next(iter(expected))
    filtered = latest_snapshot_map(session, 5, {selected_id})
    assert {security_id: snapshot.id for security_id, snapshot in filtered.items()} == {
        selected_id: expected[selected_id]
    }


def test_latest_snapshot_uses_id_only_to_break_timestamp_ties() -> None:
    metadata = MetaData()
    Base.metadata.tables[Security.__tablename__].to_metadata(metadata)
    snapshot_table = Base.metadata.tables[SecuritySignalSnapshot.__tablename__].to_metadata(
        metadata
    )
    # Imported legacy data can contain ties, unlike the current unique constraint.
    for constraint in list(snapshot_table.constraints):
        if isinstance(constraint, UniqueConstraint):
            snapshot_table.constraints.remove(constraint)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    SecuritySignalSnapshot(id=1, security_id=1, horizon=5, as_of=now),
                    SecuritySignalSnapshot(id=2, security_id=1, horizon=5, as_of=now),
                    SecuritySignalSnapshot(
                        id=3, security_id=1, horizon=5, as_of=now - timedelta(days=1)
                    ),
                ]
            )
            session.commit()
            assert latest_snapshot_map(session, 5)[1].id == 2
    finally:
        engine.dispose()

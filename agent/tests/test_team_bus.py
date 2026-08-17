from __future__ import annotations

import time

from agent.database.team_bus import SQLiteMessageBus


def test_sqlite_message_bus_claim_ack_and_release(tmp_path):
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    first = bus.send("lead", "alice", "create schema", msg_type="message")
    second = bus.send("bob", "alice", "review schema", msg_type="message")

    claimed = bus.claim_inbox("alice")
    assert [message.id for message in claimed] == [first.id, second.id]

    # A second reader cannot claim the same rows while they are already claimed.
    assert bus.claim_inbox("alice") == []

    released_count = bus.release_messages([first.id])
    assert released_count == 1
    reclaimed = bus.claim_inbox("alice")
    assert [message.id for message in reclaimed] == [first.id]

    acked_count = bus.ack_messages([first.id, second.id])
    assert acked_count == 2
    assert bus.claim_inbox("alice") == []


def test_sqlite_message_bus_releases_stale_claims(tmp_path):
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")
    message = bus.send("lead", "alice", "stale work")

    assert bus.claim_inbox("alice")[0].id == message.id

    with bus.connection() as conn:
        conn.execute(
            "UPDATE messages SET claimed_at = ? WHERE id = ?",
            (time.time() - 999, message.id),
        )

    assert bus.release_stale_claims(max_age_seconds=300) == 1
    assert bus.claim_inbox("alice")[0].id == message.id


def test_sqlite_message_bus_validates_agent_ids(tmp_path):
    bus = SQLiteMessageBus(tmp_path / "team.sqlite3")

    try:
        bus.send("lead", "../alice", "bad recipient")
    except ValueError as exc:
        assert "agent id" in str(exc)
    else:
        raise AssertionError("expected invalid agent id to fail")

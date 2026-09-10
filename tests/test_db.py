from wca.db import connect, init_schema


def test_init_schema_creates_every_table(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"audit_records", "conversations", "dedup_messages", "holds", "bookings", "escalations"} <= names


def test_init_schema_is_idempotent_and_keeps_data(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    conn.execute("INSERT INTO dedup_messages VALUES ('m1')")
    conn.commit()
    init_schema(conn)  # second call, same connection
    row = conn.execute("SELECT message_id FROM dedup_messages").fetchone()
    assert row == ("m1",)

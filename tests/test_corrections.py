"""Revision-checked corrections and their interaction with backfills."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app import backfill
from app.db import connect

from .conftest import (
    advance,
    get_sales,
    import_sales,
    replace_table,
    sale,
    sales_by_serial,
)

A_V1 = [{"region_code": "A1", "start_date": "2024-01-01", "end_date": "2024-07-01"}]
A_V2 = [{"region_code": "A2", "start_date": "2024-01-01", "end_date": "2024-07-01"}]
# Two regions so a correction's business-date change is reflected in staging.
A_SPLIT = [
    {"region_code": "JAN-EARLY", "start_date": "2024-01-01", "end_date": "2024-01-08"},
    {"region_code": "JAN-LATE", "start_date": "2024-01-08", "end_date": "2024-07-01"},
]
B_V1 = [{"region_code": "B1", "start_date": "2024-01-01", "end_date": "2024-07-01"}]
B_V2 = [{"region_code": "B2", "start_date": "2024-01-01", "end_date": "2024-07-01"}]


def correct(client: TestClient, serial_no: str, **body):
    payload = {
        "store_id": body.pop("store_id", "S1"),
        "business_date": body.pop("business_date", "2024-01-10"),
        "amount": body.pop("amount", "10.00"),
        **body,
    }
    return client.patch(f"/api/v1/sales/{serial_no}", json=payload)


def seed_table(client: TestClient, store_id: str, periods: list[dict]) -> int:
    task = replace_table(client, store_id, periods)
    assert advance(client, task["task_id"])["status"] == "completed"
    return task["target_table_id"]


def test_import_and_query_expose_revision(client: TestClient) -> None:
    result = import_sales(client, sale("R-1", "2024-01-10", "12.00"))
    assert result["results"][0]["revision"] == 1

    row = client.get("/api/v1/sales/R-1").json()
    assert row["revision"] == 1
    assert get_sales(client)["items"][0]["revision"] == 1


def test_correction_requires_observed_revision(client: TestClient) -> None:
    import_sales(client, sale("R-1", "2024-01-10", "12.00"))
    response = client.patch(
        "/api/v1/sales/R-1",
        json={"store_id": "S2", "business_date": "2024-01-11", "amount": "13.00"},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] in {"revision", "expected_revision"}

    assert client.get("/api/v1/sales/R-1").json()["revision"] == 1


def test_correction_updates_fields_and_increments_revision(client: TestClient) -> None:
    import_sales(client, sale("R-1", "2024-01-10", "12.00"))

    response = correct(
        client, "R-1", revision=1, store_id="S2", business_date="2024-02-02", amount="8.25"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["store_id"] == "S2"
    assert body["business_date"] == "2024-02-02"
    assert body["amount"] == "8.25"
    assert body["amount_cents"] == 825
    assert body["revision"] == 2

    # A fresh revision and a stale one are distinguished.
    stale = correct(client, "R-1", revision=1, amount="1.00")
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "revision_conflict"
    assert error["detail"]["expected_revision"] == 1
    assert error["detail"]["current_revision"] == 2
    assert error["detail"]["sale"]["amount"] == "8.25"

    # Failed correction left the sale untouched.
    row = client.get("/api/v1/sales/R-1").json()
    assert row["revision"] == 2
    assert row["amount"] == "8.25"
    assert row["store_id"] == "S2"

    ok = correct(client, "R-1", revision=2, amount="9.00")
    assert ok.status_code == 200
    assert ok.json()["revision"] == 3


def test_correction_unknown_serial_is_404(client: TestClient) -> None:
    response = correct(client, "NOPE", revision=1)
    assert response.status_code == 404


def test_reimport_does_not_undo_a_correction(client: TestClient) -> None:
    import_sales(client, sale("R-1", "2024-01-10", "12.00"))
    assert correct(
        client, "R-1", revision=1, business_date="2024-01-20", amount="20.00"
    ).status_code == 200

    # The original import content still hashes to the import-time content.
    replay = import_sales(client, sale("R-1", "2024-01-10", "12.00"))
    assert replay["duplicates"] == 1
    assert replay["inserted"] == 0
    assert replay["results"][0]["revision"] == 2

    # Different content under the same serial is still rejected.
    conflict = import_sales(client, sale("R-1", "2024-01-10", "99.00"))
    assert conflict["rejected"] == 1

    row = client.get("/api/v1/sales/R-1").json()
    assert row["business_date"] == "2024-01-20"
    assert row["amount"] == "20.00"
    assert row["revision"] == 2


def test_correction_attribution_uses_target_store_active_table(
    client: TestClient,
) -> None:
    a_table = seed_table(client, "S1", A_V1)
    import_sales(client, sale("R-1", "2024-01-10", "12.00"))
    row = client.get("/api/v1/sales/R-1").json()
    assert row["region_code"] == "A1"
    assert row["attribution_table_id"] == a_table

    # Target store without a table: pending immediately.
    pending = correct(client, "R-1", revision=1, store_id="S2").json()
    assert pending["attribution_status"] == "pending"
    assert pending["region_code"] is None
    assert pending["attribution_table_id"] is None

    # Target store with a table: attributed immediately, no backfill needed.
    b_table = seed_table(client, "S2", B_V1)
    moved = correct(client, "R-1", revision=2, store_id="S2").json()
    assert moved["region_code"] == "B1"
    assert moved["attribution_status"] == "attributed"
    assert moved["attribution_table_id"] == b_table
    assert moved["revision"] == 3


def test_concurrent_same_revision_corrections_accept_one(client: TestClient) -> None:
    import_sales(client, sale("R-1", "2024-01-10", "12.00"))
    results: list[int] = []
    lock = threading.Lock()

    def attempt(amount: str) -> None:
        response = correct(client, "R-1", revision=1, amount=amount)
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(amount,)) for amount in ("1.00", "2.00")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results) == [200, 409]
    row = client.get("/api/v1/sales/R-1").json()
    assert row["revision"] == 2
    assert row["amount"] in {"1.00", "2.00"}


def test_same_store_correction_updates_staged_backfill_result(
    client: TestClient, db_path
) -> None:
    seed_table(client, "S1", A_V1)
    import_sales(
        client,
        *[sale(f"S-{i}", "2024-01-10", "10.00") for i in range(5)],
    )
    task = replace_table(client, "S1", A_SPLIT)
    conn = connect(str(db_path))
    try:
        # Scan everything, so the corrected sale's id is behind the cursor.
        assert backfill.scan_batch(conn, task["task_id"], 1000).status == "ready"
        cursor_before = backfill.get_task(conn, task["task_id"])["cursor_sales_id"]

        # Same store, but a business date the corrected sale must be matched
        # against under the target table.
        response = correct(
            client,
            "S-2",
            revision=1,
            store_id="S1",
            business_date="2024-01-05",
            amount="77.00",
        )
        assert response.status_code == 200
        assert response.json()["region_code"] == "A1"

        staged = conn.execute(
            "SELECT * FROM backfill_items WHERE task_id = ? AND sales_id = "
            "(SELECT id FROM sales WHERE serial_no = 'S-2')",
            (task["task_id"],),
        ).fetchone()
        assert staged is not None
        assert staged["region_code"] == "JAN-EARLY"
        assert staged["sales_updated_at"] == response.json()["updated_at"]

        # No other sale's progress is reset.
        assert (
            backfill.get_task(conn, task["task_id"])["cursor_sales_id"]
            == cursor_before
        )
        assert backfill.commit_task(conn, task["task_id"]).status == "completed"
    finally:
        conn.close()

    row = client.get("/api/v1/sales/S-2").json()
    assert row["region_code"] == "JAN-EARLY"
    assert row["amount"] == "77.00"
    assert row["revision"] == 2
    assert sales_by_serial(client)["S-0"]["region_code"] == "JAN-LATE"


def test_same_store_correction_behind_cursor_survives_rescan(
    client: TestClient, db_path
) -> None:
    """A corrected sale behind the cursor must still win over a rewind."""
    seed_table(client, "S1", A_V1)
    import_sales(
        client,
        *[sale(f"S-{i}", "2024-01-10", "10.00") for i in range(5)],
    )
    task = replace_table(client, "S1", A_V2)
    conn = connect(str(db_path))
    try:
        assert backfill.scan_batch(conn, task["task_id"], 2).status == "running"
        # Correct a sale that has not been scanned yet.
        assert correct(
            client, "S-4", revision=1, store_id="S1", amount="44.00"
        ).status_code == 200
        assert advance(client, task["task_id"])["status"] == "completed"
    finally:
        conn.close()

    row = client.get("/api/v1/sales/S-4").json()
    assert row["amount"] == "44.00"
    assert row["region_code"] == "A2"
    assert row["revision"] == 2


def test_cross_store_correction_during_two_backfills(client: TestClient, db_path) -> None:
    """Old store must no longer rewrite the sale; new store must adopt it."""
    seed_table(client, "S1", A_V1)
    seed_table(client, "S2", B_V1)
    import_sales(
        client,
        *[sale(f"A-{i}", "2024-01-10", "10.00", store_id="S1") for i in range(5)],
    )
    task_a = replace_table(client, "S1", A_V2)

    conn = connect(str(db_path))
    try:
        # Scan past every S1 sale, including the one we are about to move, then
        # have S2 additionally stage its own sales and pass the moved id.
        assert backfill.scan_batch(conn, task_a["task_id"], 1000).status == "ready"

        import_sales(
            client,
            *[sale(f"B-{i}", "2024-01-10", "10.00", store_id="S2") for i in range(3)],
        )
        task_b = replace_table(client, "S2", B_V2)
        assert backfill.scan_batch(conn, task_b["task_id"], 1000).status == "ready"
        cursor_b = backfill.get_task(conn, task_b["task_id"])["cursor_sales_id"]

        moved = correct(
            client,
            "A-2",
            revision=1,
            store_id="S2",
            business_date="2024-01-15",
            amount="66.00",
        )
        assert moved.status_code == 200
        # Immediately attributed against S2's *active* table (B1), not B2.
        assert moved.json()["region_code"] == "B1"
        moved_id = conn.execute(
            "SELECT id FROM sales WHERE serial_no = 'A-2'"
        ).fetchone()["id"]
        assert moved_id <= cursor_b  # the new store's scan already passed it

        # Old store no longer stages the moved sale at all.
        assert (
            conn.execute(
                "SELECT 1 FROM backfill_items WHERE task_id = ? AND sales_id = ?",
                (task_a["task_id"], moved_id),
            ).fetchone()
            is None
        )
        staged_b = conn.execute(
            "SELECT * FROM backfill_items WHERE task_id = ? AND sales_id = ?",
            (task_b["task_id"], moved_id),
        ).fetchone()
        assert staged_b is not None and staged_b["region_code"] == "B2"

        state_a = backfill.task_to_dict(backfill.get_task(conn, task_a["task_id"]))
        assert state_a["total_sales"] == state_a["processed_sales"] == 4
        state_b = backfill.task_to_dict(backfill.get_task(conn, task_b["task_id"]))
        assert state_b["total_sales"] == state_b["processed_sales"] == 4

        # Both tasks complete normally; the old store must not overwrite.
        assert backfill.commit_task(conn, task_a["task_id"]).status == "completed"
        assert backfill.commit_task(conn, task_b["task_id"]).status == "completed"
    finally:
        conn.close()

    rows = sales_by_serial(client)
    assert rows["A-2"]["store_id"] == "S2"
    assert rows["A-2"]["amount"] == "66.00"
    assert rows["A-2"]["region_code"] == "B2"
    assert rows["A-2"]["attribution_table_id"] == task_b["target_table_id"]
    assert rows["A-0"]["region_code"] == "A2"
    assert rows["B-0"]["region_code"] == "B2"


def test_correction_into_store_with_unfinished_task_and_no_prior_sales(
    client: TestClient, db_path
) -> None:
    """Sale enters a store whose backfill is running but has no sales yet."""
    seed_table(client, "S1", A_V1)
    import_sales(
        client, *[sale(f"A-{i}", "2024-01-10", "10.00", store_id="S1") for i in range(3)]
    )
    task_b = replace_table(client, "S2", B_V2)
    conn = connect(str(db_path))
    try:
        assert backfill.scan_batch(conn, task_b["task_id"], 1000).status == "ready"

        moved = correct(client, "A-1", revision=1, store_id="S2", amount="5.00")
        assert moved.status_code == 200
        # No active table for S2 yet -> pending until the task commits.
        assert moved.json()["attribution_status"] == "pending"

        # The task had scanned an empty store; the moved sale reopens the scan
        # and is staged before the commit succeeds.
        assert advance(client, task_b["task_id"])["status"] == "completed"
    finally:
        conn.close()

    row = client.get("/api/v1/sales/A-1").json()
    assert row["store_id"] == "S2"
    assert row["region_code"] == "B2"
    assert row["attribution_table_id"] == task_b["target_table_id"]


def test_failed_correction_preserves_backfill_state(
    client: TestClient, db_path
) -> None:
    seed_table(client, "S1", A_V1)
    import_sales(client, sale("A-0", "2024-01-10", "10.00"))
    task = replace_table(client, "S1", A_V2)
    conn = connect(str(db_path))
    try:
        assert backfill.scan_batch(conn, task["task_id"], 0 or 1).status == "ready"
        before_items = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM backfill_items WHERE task_id = ? ORDER BY sales_id",
                (task["task_id"],),
            )
        ]
        before_task = backfill.task_to_dict(backfill.get_task(conn, task["task_id"]))
        before_sale = client.get("/api/v1/sales/A-0").json()

        bad = correct(client, "A-0", revision=99, amount="1.00")
        assert bad.status_code == 409

        after_items = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM backfill_items WHERE task_id = ? ORDER BY sales_id",
                (task["task_id"],),
            )
        ]
        assert after_items == before_items
        assert (
            backfill.task_to_dict(backfill.get_task(conn, task["task_id"]))
            == before_task
        )
        assert client.get("/api/v1/sales/A-0").json() == before_sale
    finally:
        conn.close()


def test_correction_while_old_task_is_failed_cannot_be_overwritten(
    client: TestClient, db_path
) -> None:
    seed_table(client, "S1", A_V1)
    seed_table(client, "S2", B_V1)
    import_sales(client, sale("A-0", "2024-01-10", "10.00", store_id="S1"))
    old = replace_table(client, "S1", A_V2)

    conn = connect(str(db_path))
    try:
        assert backfill.scan_batch(conn, old["task_id"], 1000).status == "ready"
        for _ in range(backfill.MAX_ATTEMPTS):
            backfill.record_failure(conn, old["task_id"], "boom")
        assert backfill.get_task(conn, old["task_id"])["status"] == "failed"

        moved = correct(client, "A-0", revision=1, store_id="S2")
        assert moved.status_code == 200

        # Retrying the failed old task must not resurrect the sale into S1.
        retried = client.post(f"/api/v1/backfill-tasks/{old['task_id']}/retry")
        assert retried.status_code == 200
        assert advance(client, old["task_id"])["status"] == "completed"
    finally:
        conn.close()

    row = client.get("/api/v1/sales/A-0").json()
    assert row["store_id"] == "S2"
    assert row["region_code"] == "B1"
    assert row["revision"] == 2


def test_migration_adds_revision_to_existing_database(tmp_path) -> None:
    """Databases created by the previous release get revision=1 backfilled."""
    import sqlite3

    from app.db import init_db

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_no TEXT NOT NULL UNIQUE,
            store_id TEXT NOT NULL,
            business_date TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            region_code TEXT,
            attribution_status TEXT NOT NULL,
            attribution_table_id INTEGER,
            imported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO sales(serial_no, store_id, business_date, amount_cents,
                          content_hash, attribution_status, imported_at, updated_at)
        VALUES ('OLD', 'S1', '2024-01-01', 100, 'h', 'pending', 't', 't');
        """
    )
    legacy.close()

    conn = connect(str(path))
    try:
        init_db(conn)
        row = conn.execute("SELECT revision FROM sales WHERE serial_no='OLD'").fetchone()
        assert row["revision"] == 1
        # Idempotent.
        init_db(conn)
    finally:
        conn.close()

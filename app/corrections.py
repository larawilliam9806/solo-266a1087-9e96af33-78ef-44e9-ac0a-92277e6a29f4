"""Revision-checked corrections of imported sales.

A correction replaces the store / business date / amount of an existing sale.
It is guarded by an optimistic-concurrency revision: the caller must send the
revision it observed, and a successful correction increments it.  Two
corrections racing on the same revision therefore cannot both apply.

Corrections also have to stay coherent with in-flight backfills:

* The sale is re-attributed immediately against the *target store's* currently
  active region table (``pending`` when nothing matches).
* Any unfinished backfill task that no longer owns the sale (the old store, or
  an even earlier one) drops its staged row, so its eventual commit can never
  rewrite the corrected sale.
* Any unfinished backfill task that now owns the sale gets a staged row for the
  corrected data — inserted even when the task's cursor already moved past
  this ``sales.id``.  Cursors and staged rows of *other* sales are left
  untouched, so no other sale's backfill progress is reset.

Everything happens under one ``BEGIN IMMEDIATE`` transaction: a rejected
correction leaves the sale, its revision and the backfill state exactly as they
were.
"""

from __future__ import annotations

import sqlite3

from .backfill import refresh_counts
from .db import utc_now_iso, write_tx
from .errors import NotFoundError, RevisionConflictError
from .models import NormalisedCorrection
from .sales import active_region_table_id, ensure_store, match_region


def correct_sale(
    conn: sqlite3.Connection, serial_no: str, correction: NormalisedCorrection
) -> sqlite3.Row:
    """Apply ``correction`` to ``serial_no`` and return the updated row.

    Raises ``NotFoundError`` when the serial does not exist and
    ``RevisionConflictError`` when the observed revision is stale.
    """
    with write_tx(conn):
        current = conn.execute(
            "SELECT * FROM sales WHERE serial_no = ?", (serial_no,)
        ).fetchone()
        if current is None:
            raise NotFoundError("sale not found", detail={"serial_no": serial_no})
        if current["revision"] != correction.revision:
            raise RevisionConflictError(
                "sale revision does not match the observed revision",
                detail={
                    "serial_no": serial_no,
                    "expected_revision": correction.revision,
                    "current_revision": current["revision"],
                },
            )

        store_id = correction.store_id
        business_date = correction.business_date
        amount_cents = correction.amount_cents
        ensure_store(conn, store_id)

        # Re-attribute right away against whatever table is active for the
        # *target* store now.  If that store has an unfinished replacement
        # backfill, this is its source table; the commit below will later apply
        # the corrected data under the target table.
        table_id = active_region_table_id(conn, store_id)
        region = match_region(conn, table_id, business_date) if table_id else None
        status = "attributed" if region else "pending"
        now = utc_now_iso()

        cursor = conn.execute(
            """
            UPDATE sales
            SET store_id = ?, business_date = ?, amount_cents = ?,
                revision = revision + 1,
                region_code = ?, attribution_status = ?, attribution_table_id = ?,
                updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                store_id,
                business_date,
                amount_cents,
                region,
                status,
                table_id,
                now,
                current["id"],
                correction.revision,
            ),
        )
        if cursor.rowcount != 1:  # pragma: no cover - guarded by the write lock
            raise RevisionConflictError(
                "sale was corrected concurrently",
                detail={
                    "serial_no": serial_no,
                    "expected_revision": correction.revision,
                },
            )

        updated = conn.execute(
            "SELECT * FROM sales WHERE id = ?", (current["id"],)
        ).fetchone()
        _sync_backfill_items(conn, updated, old_store_id=current["store_id"])
        return updated


def _sync_backfill_items(
    conn: sqlite3.Connection, sale: sqlite3.Row, *, old_store_id: str
) -> None:
    """Move the sale's staged rows between unfinished backfill tasks.

    ``failed`` tasks are included: they may still be retried and committed
    later, so leaving a stale staged row behind would let the old store's
    table overwrite the correction.
    """
    tasks = conn.execute(
        """
        SELECT id, store_id, target_table_id
        FROM backfill_tasks AS t
        WHERE t.status <> 'completed'
          AND (
              t.store_id IN (?, ?)
              OR EXISTS (
                  SELECT 1 FROM backfill_items b
                  WHERE b.task_id = t.id AND b.sales_id = ?
              )
          )
        """,
        (old_store_id, sale["store_id"], sale["id"]),
    ).fetchall()
    for task in tasks:
        if task["store_id"] != sale["store_id"]:
            # The sale left this store (or passed through it earlier).  Drop
            # any staged result so a later commit cannot overwrite the
            # corrected sale.  The scan only selects current store members, so
            # no cursor movement is needed.
            conn.execute(
                "DELETE FROM backfill_items WHERE task_id = ? AND sales_id = ?",
                (task["id"], sale["id"]),
            )
        else:
            region = match_region(
                conn, task["target_table_id"], sale["business_date"]
            )
            status = "attributed" if region else "pending"
            # Upsert regardless of the cursor position: a sale corrected into
            # this store may have an id the scan already passed, and a
            # same-store correction must replace the staged value.  Neither
            # path moves ``cursor_sales_id``, so no other sale's progress is
            # reset.
            conn.execute(
                """
                INSERT INTO backfill_items(
                    task_id, sales_id, sales_updated_at, region_code,
                    attribution_status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, sales_id) DO UPDATE SET
                    sales_updated_at = excluded.sales_updated_at,
                    region_code = excluded.region_code,
                    attribution_status = excluded.attribution_status
                """,
                (
                    task["id"],
                    sale["id"],
                    sale["updated_at"],
                    region,
                    status,
                ),
            )
        # Keep both stores' progress counters truthful: the moved sale changes
        # the total of the old store and the processed count of the new one.
        refresh_counts(conn, task["id"], task["store_id"])

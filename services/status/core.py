"""Everything the upload page shows about one batch, in one query.

Why one query: the page polls this while the pipeline runs, and from India every request to
ap-southeast-2 costs a ~400 ms round trip (docs/DECISIONS.md AD27). So one call returns the
whole timeline: the batch row, each stage's counts from pipeline_metrics, and the email
outcome, with the durations already worked out.
"""

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

STATUS_SQL = """
SELECT b.batch_id::text AS batch_id, b.s3_key, b.status, b.rows_total, b.rows_valid,
       b.rows_rejected, b.rejects_key, b.error,
       b.received_at, b.ingested_at, b.matching_at, b.matched_at, b.notified_at, b.failed_at,
       coalesce((SELECT jsonb_object_agg(m.stage, jsonb_build_object(
                            'pairs_in', m.pairs_in, 'pairs_out', m.pairs_out,
                            'llm_calls', m.llm_calls, 'ms', m.duration_ms, 'details', m.details)
                        ORDER BY m.metric_id)
                 FROM pipeline_metrics m WHERE m.batch_id = b.batch_id), '{}'::jsonb) AS stages,
       coalesce((SELECT jsonb_object_agg(n.status, n.count)
                 FROM (SELECT status, count(*) AS count FROM notifications
                       WHERE batch_id = b.batch_id GROUP BY status) n), '{}'::jsonb) AS emails
FROM ingestion_batches b
WHERE b.s3_key = %(key)s OR b.batch_id::text = %(batch)s
ORDER BY b.received_at DESC
LIMIT 1
"""

TERMINAL = {"notified", "failed"}


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def batch_status(
    conn: psycopg.Connection, *, key: str | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    """The batch for an upload key (or batch id), or {status: 'uploaded'} until ingest starts."""
    with conn.cursor(row_factory=dict_row) as cur:
        data = cur.execute(STATUS_SQL, {"key": key, "batch": batch_id}).fetchone()
    if data is None:
        return {"status": "uploaded", "key": key, "done": False}
    stages: dict[str, Any] = data.pop("stages")
    write = stages.get("write", {})
    hard_filter = stages.get("s2_hard_filter", {})
    plan = stages.get("plan_notifications", {})
    llm_calls = sum(int(s.get("llm_calls") or 0) for s in stages.values())
    out: dict[str, Any] = {
        "batch_id": data["batch_id"],
        "key": data["s3_key"],
        "status": data["status"],
        "done": data["status"] in TERMINAL,
        "error": data["error"],
        "email_mode": (plan.get("details") or {}).get("email_mode"),
        "rows": {
            "total": data["rows_total"],
            "valid": data["rows_valid"],
            "rejected": data["rows_rejected"],
            "rejects_key": data["rejects_key"],
        },
        "matching": {
            "pairs": write.get("pairs_in"),
            "by_status": {
                k: v
                for k, v in (write.get("details") or {}).items()
                if k in ("matched", "llm_matched", "llm_rejected", "pending_llm")
            },
            "llm_calls": llm_calls,
            "customers_with_offers": (write.get("details") or {}).get("users_with_offers"),
            "products": (hard_filter.get("details") or {}).get("active_products"),
        },
        "emails": {**data["emails"], "to_send": plan.get("pairs_out")},
        "ms": {
            "ingest": _ms(data["received_at"], data["ingested_at"]),
            "wait_for_match": _ms(data["ingested_at"], data["matching_at"]),
            "match": _ms(data["matching_at"], data["matched_at"]),
            "notify": _ms(data["matched_at"], data["notified_at"]),
            "total": _ms(data["received_at"], data["notified_at"] or data["failed_at"]),
        },
    }
    return out

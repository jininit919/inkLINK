"""Telemetry event emission.

We log structured events to a `telemetry_events` table for the net-take-rate
dashboard and post-hoc analysis. NOT a replacement for Sentry/error logs —
this is for business events with JSON payloads.

Why our own table vs. external service: zero deps, queryable via SQL with
the rest of the data, no PII leak risk.
"""
import json
import sys
from datetime import datetime
from typing import Optional, Any


def emit_event(event_name: str, payload: dict, conn=None) -> None:
    """Records a business event with a JSON payload.

    Examples of event_name:
    - booking.economics_calculated  (at PaymentIntent creation)
    - booking.completed             (both sides confirmed, payout queued)
    - booking.refunded              (refund processed)
    - booking.disputed              (dispute opened)
    - discount.applied              (a discount was successfully applied)
    - discount.rejected             (validation failed, with error_code)

    The `conn` parameter is a DB connection. If None, the event is printed
    to stderr (useful for tests/local). The DB write is best-effort —
    logging failures do not propagate.
    """
    ts = datetime.utcnow().isoformat()
    safe_payload = _safe_jsonable(payload)

    if conn is None:
        print(f'[telemetry] {ts} {event_name} {json.dumps(safe_payload)}',
              file=sys.stderr)
        return

    try:
        conn.execute(
            'INSERT INTO telemetry_events (event_name, payload_json, created_at) VALUES (?, ?, ?)',
            (event_name, json.dumps(safe_payload, ensure_ascii=False), ts),
        )
        # Caller is responsible for conn.commit() — we don't commit here to
        # avoid breaking transaction boundaries.
    except Exception as e:
        # Logging must NEVER break a request. Fall back to stderr.
        print(f'[telemetry-error] {e}; event={event_name}', file=sys.stderr)


def _safe_jsonable(x: Any) -> Any:
    """Recursively coerce Decimal/datetime to JSON-safe primitives."""
    from decimal import Decimal
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: _safe_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_safe_jsonable(v) for v in x]
    return x

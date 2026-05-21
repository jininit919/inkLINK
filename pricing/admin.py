"""Admin KPI query — single function returning net-take-rate dashboard data.

Reads from economics_snapshots (kind='initial' = completed bookings, kind='refund'
= refund losses). Filterable by date range, city, artist tier.

Why a function (not raw SQL): we want the same numbers in the dashboard
and in any internal reports. Centralizing prevents drift.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional


def admin_kpis(conn, start: datetime, end: datetime,
               city: Optional[str] = None,
               artist_tier: Optional[str] = None) -> dict:
    """Returns KPI dict for the given date range.

    artist_tier ∈ {'founding', 'standard'} or None for all.
    Date filter is on bookings.completed_at (we report completed bookings only).
    """
    # Build the artist filter into the query
    artist_filter_sql = ''
    artist_filter_args = []
    if artist_tier == 'founding':
        artist_filter_sql = 'AND a.founding_artist = 1'
    elif artist_tier == 'standard':
        artist_filter_sql = 'AND (a.founding_artist = 0 OR a.founding_artist IS NULL)'

    city_filter_sql = ''
    city_filter_args = []
    if city:
        city_filter_sql = 'AND LOWER(a.city) = LOWER(?)'
        city_filter_args = [city]

    # Sum up snapshot fields for completed bookings in range.
    # We can't aggregate JSON in standard SQL portably, so we pull the rows
    # and aggregate in Python. For dashboards with thousands of bookings
    # this is fine; for millions, denormalize into snapshot columns later.
    rows = conn.execute(f'''
        SELECT es.snapshot, es.kind, b.id, b.completed_at
        FROM bookings b
        JOIN economics_snapshots es ON es.booking_id = b.id
        JOIN users a ON a.id = b.artist_id
        WHERE b.status = 'completed'
          AND b.completed_at IS NOT NULL
          AND b.completed_at >= ?
          AND b.completed_at <  ?
          {artist_filter_sql}
          {city_filter_sql}
        ORDER BY b.completed_at
    ''', [start.isoformat(), end.isoformat()] + artist_filter_args + city_filter_args).fetchall()

    gross_gmv     = Decimal('0')
    net_revenue   = Decimal('0')
    discount_cost = Decimal('0')
    stripe_cost   = Decimal('0')
    refund_loss   = Decimal('0')
    initial_count = 0

    seen_initial = set()
    for r in rows:
        try:
            snap = json.loads(r['snapshot'])
        except Exception:
            continue
        bid = r['id']
        if r['kind'] == 'initial':
            if bid in seen_initial:
                continue  # safety against duplicates
            seen_initial.add(bid)
            initial_count += 1
            gross_gmv     += Decimal(str(snap.get('gross_price', 0)))
            net_revenue   += Decimal(str(snap.get('inklink_net', 0)))
            discount_cost += Decimal(str(snap.get('discount_applied', 0)))
            stripe_cost   += Decimal(str(snap.get('stripe_fee', 0)))
        elif r['kind'] == 'refund':
            # Refund snapshots store the LOSS as a negative inklink_net.
            net_loss = Decimal(str(snap.get('refund_loss_czk', 0)))
            refund_loss   += net_loss
            net_revenue   -= net_loss

    take_rate = (net_revenue / gross_gmv) if gross_gmv > 0 else Decimal('0')
    avg_price = (gross_gmv / Decimal(initial_count)) if initial_count > 0 else Decimal('0')

    return {
        'range': {'start': start.isoformat(), 'end': end.isoformat()},
        'filters': {'city': city, 'artist_tier': artist_tier},
        'gross_gmv_czk':      float(gross_gmv),
        'net_revenue_czk':    float(net_revenue),
        'net_take_rate':      float(round(take_rate, 4)),
        'completed_count':    initial_count,
        'discount_cost_czk':  float(discount_cost),
        'stripe_cost_czk':    float(stripe_cost),
        'refund_loss_czk':    float(refund_loss),
        'avg_gross_price_czk': float(round(avg_price, 0)),
    }


def admin_kpis_last_30d(conn, **kwargs) -> dict:
    """Convenience wrapper — last 30 days from now."""
    end = datetime.utcnow()
    start = end - timedelta(days=30)
    return admin_kpis(conn, start, end, **kwargs)

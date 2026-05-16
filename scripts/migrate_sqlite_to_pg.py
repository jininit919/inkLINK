#!/usr/bin/env python3
"""
Migrate data from SQLite (inklink.db) → PostgreSQL (DATABASE_URL).

Použití:
    SQLITE_PATH=inklink.db DATABASE_URL=postgresql://... python scripts/migrate_sqlite_to_pg.py

Skript:
1. Načte schéma z SQLite (zjistí všechny tabulky a sloupce).
2. Pro každou tabulku zavolá init_db() z server.py (ten už vytvoří tabulky v PG).
3. Vyprázdní cílové tabulky (POZOR — destruktivní pro PG).
4. Postupně kopíruje řádky ze SQLite do PG.
5. Resetuje sekvence (auto-incrementy) na max(id)+1.

Bezpečnostní zámky:
- Vyžaduje env var CONFIRM_MIGRATE=YES (jinak končí bez práce).
- Neumí běžet, pokud DATABASE_URL nepřipomíná postgres.
- Vypisuje co dělá, počítá řádky, na konci summary.
"""

import os
import sys
import sqlite3
from pathlib import Path

# Vlož projekt do PYTHONPATH, aby šel importovat server.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SQLITE_PATH = os.environ.get('SQLITE_PATH', str(ROOT / 'inklink.db'))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
CONFIRM = os.environ.get('CONFIRM_MIGRATE', '') == 'YES'

# ── Pořadí tabulek (FK závislosti) ────────────────────────────────────────────
# Tabulky bez závislostí první → ty co odkazují na ostatní později.
TABLE_ORDER = [
    'users',
    'follows',
    'messages',
    'favorite_cities',
    'events',
    'event_saves',
    'notifications',
    'push_subscriptions',
    'password_reset_tokens',
    'portfolio_items',
    'portfolio_likes',
    'slots',
    'bookings',
    'reviews',
    'review_reports',
]


def die(msg, code=1):
    print(f'❌ {msg}', file=sys.stderr)
    sys.exit(code)


def main():
    if not CONFIRM:
        die('Nastav CONFIRM_MIGRATE=YES pro spuštění migrace.\n'
            '   Skript vyprázdní cílové tabulky v Postgres a překopíruje data ze SQLite.')

    if not DATABASE_URL or 'postgres' not in DATABASE_URL.lower():
        die(f'DATABASE_URL musí být postgres URL. Aktuálně: {DATABASE_URL!r}')

    if not Path(SQLITE_PATH).exists():
        die(f'SQLite soubor nenalezen: {SQLITE_PATH}')

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        die('Chybí psycopg2-binary. `pip install psycopg2-binary`')

    # 1. Připoj se k oběma DB
    print(f'📂 SQLite:   {SQLITE_PATH}')
    print(f'🐘 Postgres: {DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL}')
    print()

    sq = sqlite3.connect(SQLITE_PATH)
    sq.row_factory = sqlite3.Row

    pg = psycopg2.connect(DATABASE_URL)
    pg.autocommit = False
    pg_cur = pg.cursor()

    # 2. Spusť init_db() v PG kontextu, ať existují tabulky
    print('🛠  Vytvářím tabulky v Postgres (init_db)…')
    os.environ['DATABASE_URL'] = DATABASE_URL  # zaručí, že server.get_db() použije PG
    # Reload tak, aby DATABASE_URL byl viděn:
    import importlib
    import server
    importlib.reload(server)
    server.init_db()
    print('   ✓ schéma připraveno')
    print()

    # 3. Pro každou tabulku zjistit sloupce + překopírovat
    summary = []
    total = 0

    for table in TABLE_ORDER:
        # Existuje tabulka v SQLite?
        try:
            cols_info = sq.execute(f'PRAGMA table_info({table})').fetchall()
        except sqlite3.OperationalError:
            print(f'⚠  {table}: neexistuje v SQLite, přeskakuji')
            summary.append((table, 0, 'skip (no source)'))
            continue
        if not cols_info:
            print(f'⚠  {table}: neexistuje v SQLite, přeskakuji')
            summary.append((table, 0, 'skip (no source)'))
            continue

        cols = [c['name'] for c in cols_info]
        rows = sq.execute(f'SELECT {",".join(cols)} FROM {table}').fetchall()
        n = len(rows)

        if n == 0:
            print(f'·  {table}: 0 řádků, přeskakuji')
            summary.append((table, 0, 'empty'))
            continue

        # Vyprázdnit cílovou tabulku
        try:
            pg_cur.execute(f'TRUNCATE TABLE {table} RESTART IDENTITY CASCADE')
        except psycopg2.Error as e:
            pg.rollback()
            print(f'⚠  {table}: TRUNCATE selhal ({e}), zkouším DELETE')
            pg_cur.execute(f'DELETE FROM {table}')

        # Insert po batchích
        placeholders = ','.join(['%s'] * len(cols))
        col_list     = ','.join(cols)
        insert_sql   = f'INSERT INTO {table} ({col_list}) VALUES ({placeholders})'

        # Konverze sqlite3.Row → tuple (a None pro NULL)
        batch = []
        BATCH_SIZE = 500
        copied = 0
        for r in rows:
            batch.append(tuple(r[c] for c in cols))
            if len(batch) >= BATCH_SIZE:
                psycopg2.extras.execute_batch(pg_cur, insert_sql, batch)
                copied += len(batch)
                batch = []
        if batch:
            psycopg2.extras.execute_batch(pg_cur, insert_sql, batch)
            copied += len(batch)

        # Resetuj sekvenci (auto-increment) na max(id)+1, pokud má sloupec 'id'
        if 'id' in cols:
            try:
                pg_cur.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1))",
                    (table,),
                )
            except psycopg2.Error as e:
                print(f'   ⚠ sekvence pro {table}.id nešla resetovat: {e}')

        pg.commit()
        print(f'✓  {table}: zkopírováno {copied} řádků')
        summary.append((table, copied, 'ok'))
        total += copied

    sq.close()
    pg_cur.close()
    pg.close()

    # 4. Summary
    print()
    print('═' * 56)
    print(' MIGRACE DOKONČENA')
    print('═' * 56)
    for table, count, status in summary:
        marker = '✓' if status == 'ok' else ('·' if status == 'empty' else '⚠')
        print(f'  {marker} {table:<28} {count:>7}  ({status})')
    print('─' * 56)
    print(f'  Celkem: {total} řádků')
    print('═' * 56)


if __name__ == '__main__':
    main()

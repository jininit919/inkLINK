#!/usr/bin/env python3
"""
Quick health-check: ověří, že DATABASE_URL funguje a server.py se k němu připojí.

Použití (local):
    DATABASE_URL=postgresql://... python scripts/check_postgres.py

Použití (Railway):
    railway run python scripts/check_postgres.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    db_url = os.environ.get('DATABASE_URL', '')
    if not db_url:
        print('❌ DATABASE_URL není nastavena.')
        sys.exit(1)
    if 'postgres' not in db_url.lower():
        print(f'⚠  DATABASE_URL nevypadá jako postgres: {db_url[:40]}...')

    print(f'🐘 Připojuji se k: {db_url.split("@")[-1] if "@" in db_url else db_url[:30]}…')

    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute('SELECT version()')
        version = cur.fetchone()[0]
        print(f'✓ Připojeno. Server: {version.split(",")[0]}')

        # Zkus zavolat init_db ze server.py
        import server
        server.init_db()
        print('✓ init_db() proběhl — tabulky existují / vytvořeny.')

        # Spočítej řádky v hlavních tabulkách
        for table in ['users', 'portfolio_items', 'bookings', 'messages']:
            try:
                cur.execute(f'SELECT COUNT(*) FROM {table}')
                n = cur.fetchone()[0]
                print(f'   {table}: {n} řádků')
            except Exception as e:
                print(f'   {table}: chyba — {e}')

        cur.close()
        conn.close()
        print()
        print('🎉 Vše OK — můžeš migrovat (nebo rovnou switchnout app na Postgres).')

    except Exception as e:
        print(f'❌ Chyba: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()

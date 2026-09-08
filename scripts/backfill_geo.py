#!/usr/bin/env python3
"""Doplní souřadnice tatérům, kteří je nemají.

Souřadnice se počítají při ukládání profilu. Účty založené dřív, než
geokódování existovalo, je proto nemají — a na mapě nejsou, protože
/api/artists/map vrací jen ty s vyplněným lat/lng. Tenhle skript je
dopočítá pozpátku ze stejné funkce, jakou používá ukládání profilu,
takže výsledek je totožný.

Použití:
    python scripts/backfill_geo.py            # jen vypíše, co by udělal
    python scripts/backfill_geo.py --apply    # zapíše
    python scripts/backfill_geo.py --apply --limit 20

Na Railway přes `railway run python scripts/backfill_geo.py --apply`,
aby skript viděl DATABASE_URL produkční databáze.

Pouštět se dá opakovaně: bere jen tatéry bez souřadnic, takže druhý běh
zpracuje jen ty, na kterých ten první selhal.

Nominatim pouští jeden dotaz za vteřinu. Odpovědi drží `geo_cache`, takže
deset tatérů z Prahy stojí jeden dotaz — pauza se čeká jen tehdy, když se
opravdu šlo na síť.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOMINATIM_GAP_S = 1.1     # limit je 1 dotaz/s, s rezervou


def cached(conn, studio_address, city):
    """Je odpověď na tenhle profil už v cache? Podle toho se čeká pauza."""
    import server
    for q in server._geocode_queries(studio_address, city):
        row = conn.execute('SELECT 1 FROM geo_cache WHERE query=?',
                           (q.lower(),)).fetchone()
        if row:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='zapsat do databáze (bez toho jen výpis)')
    ap.add_argument('--limit', type=int, default=0,
                    help='zpracovat nejvýš N tatérů')
    args = ap.parse_args()

    import server

    conn = server.get_db()
    rows = conn.execute(
        "SELECT id, username, display_name, city, studio_address "
        "FROM users WHERE is_artist = 1 AND (lat IS NULL OR lng IS NULL) "
        "ORDER BY id").fetchall()
    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        print('Všichni tatéři už souřadnice mají — není co doplňovat.')
        conn.close()
        return 0

    print(f'{"ZKOUŠKA (nic se nezapíše)" if not args.apply else "ZÁPIS"} — '
          f'{len(rows)} tatérů bez souřadnic\n')

    done = skipped = failed = 0
    for r in rows:
        city = (r['city'] or '').strip()
        addr = (r['studio_address'] or '').strip()
        who = f"#{r['id']} {r['display_name'] or r['username']}"

        if not city and not addr:
            # Bez města i adresy není z čeho počítat. Není to chyba —
            # tatér prostě nevyplnil, kde působí.
            print(f'  {who:<34} — bez města i adresy, přeskočeno')
            skipped += 1
            continue

        was_cached = cached(conn, addr, city)
        hit = server._geocode_profile(addr, city)
        if not was_cached:
            time.sleep(NOMINATIM_GAP_S)

        if not hit:
            print(f'  {who:<34} ✗ nenalezeno: {addr or city}')
            failed += 1
            continue

        lat, lng = hit
        src = 'adresa' if addr else 'město'
        print(f'  {who:<34} → {lat:.5f}, {lng:.5f}  ({src}: {addr or city})')
        if args.apply:
            conn.execute('UPDATE users SET lat=?, lng=? WHERE id=?',
                         (lat, lng, r['id']))
            conn.commit()
        done += 1

    conn.close()
    print(f'\nDoplněno {done}, přeskočeno {skipped}, nenalezeno {failed}.')
    if not args.apply and done:
        print('Nic se nezapsalo. Pro zápis spusť znovu s --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

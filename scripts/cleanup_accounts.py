#!/usr/bin/env python3
"""Úklid testovacích účtů z veřejného adresáře.

Testovací a demo účty se na produkci nasbíraly během vývoje a od chvíle,
kdy je web veřejný, je vidí každý — včetně recenzenta Mety. Tenhle skript
je z adresáře odklidí.

Nemaže. Jen sundá příznak tatéra (`is_artist = 0`), což účet okamžitě
vyřadí z adresáře, hledání, feedu, mapy i sitemapy. Přihlásit se dá dál
a jedním příkazem to jde vrátit — na rozdíl od mazání, které je nevratné
a bere s sebou i portfolio.

Použití (v Railway konzoli služby INKLINK):
    cd /app && /opt/venv/bin/python scripts/cleanup_accounts.py list
    cd /app && /opt/venv/bin/python scripts/cleanup_accounts.py hide --user demoartist
    cd /app && /opt/venv/bin/python scripts/cleanup_accounts.py hide --user demoartist --apply
    cd /app && /opt/venv/bin/python scripts/cleanup_accounts.py show --user demoartist --apply

Bez `--apply` se jen vypíše, co by se stalo.

Účet `meta_review` nech být, dokud neproběhne App Review — recenzent se
jím přihlašuje a potřebuje vidět profil tatéra.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Jména, která se za tatéra vydávat nemají. Není to mazací seznam, jen
# nápověda ve výpisu — rozhodnutí zůstává na člověku.
SUSPECT = ('test', 'demo', 'zkouska', 'zkouška', 'klienttest', 'qwk', 'foo', 'bar')


def _je(n):
    if n == 0:
        return 'není žádný tatér'
    if n == 1:
        return 'je 1 tatér'
    return f'jsou {n} tatéři' if n < 5 else f'je {n} tatérů'


def _klienti(n):
    if n == 0:
        return 'Žádné další účty tu nejsou'
    if n == 1:
        return 'Jeden další účet je klient'
    return (f'Další {n} účty jsou klienti' if n < 5
            else f'Dalších {n} účtů jsou klienti')


def looks_like_test(username, display_name):
    blob = f'{username} {display_name or ""}'.lower()
    return any(w in blob for w in SUSPECT)


def cmd_list(args, server):
    conn = server.get_db()
    rows = conn.execute(
        'SELECT id, username, display_name, city, is_artist, lat, '
        '       (SELECT COUNT(*) FROM portfolio_items WHERE user_id = users.id) AS prace '
        'FROM users WHERE deleted_at IS NULL ORDER BY is_artist DESC, id'
    ).fetchall()
    conn.close()

    artists = [r for r in rows if r['is_artist']]
    others = [r for r in rows if not r['is_artist']]

    print(f'Ve veřejném adresáři {_je(len(artists))}:\n')
    for r in artists:
        mark = '  ← vypadá jako testovací' if looks_like_test(
            r['username'], r['display_name']) else ''
        # Dvě různé příčiny, dvě různé opravy: bez města nemá geokódování
        # z čeho počítat, s městem stačí dopočítat souřadnice zpětně.
        if r['lat'] is not None:
            mapa = 'na mapě'
        elif not (r['city'] or '').strip():
            mapa = 'MIMO MAPU — chybí město'
        else:
            mapa = 'MIMO MAPU — chybí souřadnice, viz backfill_geo.py'
        print(f'  #{r["id"]:<4} @{r["username"]:<16} {(r["display_name"] or ""):<22} '
              f'{(r["city"] or "—"):<10} {r["prace"]} prací · {mapa}{mark}')

    print('\n' + _klienti(len(others)) +
          ' — ti se v adresáři neukazují.')
    print('\nSkrytí: cleanup_accounts.py hide --user <jméno> --apply')
    return 0


def _set_artist(args, server, value, slovo):
    conn = server.get_db()
    row = conn.execute(
        'SELECT id, username, display_name, is_artist FROM users '
        'WHERE username = ? AND deleted_at IS NULL', (args.user,)).fetchone()
    if not row:
        print(f'Účet @{args.user} neexistuje.')
        conn.close()
        return 1

    if bool(row['is_artist']) == bool(value):
        print(f'@{row["username"]} už {slovo} je — není co dělat.')
        conn.close()
        return 0

    print(f'{"ZKOUŠKA (nic se nezapíše)" if not args.apply else "ZÁPIS"} — '
          f'@{row["username"]} ({row["display_name"]}) → {slovo}')

    if not args.apply:
        conn.close()
        print('\nNic se nezapsalo. Pro zápis spusť znovu s --apply.')
        return 0

    conn.execute('UPDATE users SET is_artist = ? WHERE id = ?', (value, row['id']))
    conn.commit()
    conn.close()
    print('\nHotovo. Portfolio ani přihlášení se nemění, jde to vrátit '
          'příkazem show.')
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('list', help='vypsat, kdo je ve veřejném adresáři')

    h = sub.add_parser('hide', help='sundat příznak tatéra (pryč z adresáře)')
    h.add_argument('--user', required=True)
    h.add_argument('--apply', action='store_true')

    s = sub.add_parser('show', help='vrátit příznak tatéra')
    s.add_argument('--user', required=True)
    s.add_argument('--apply', action='store_true')

    args = ap.parse_args()
    import server
    if args.cmd == 'list':
        return cmd_list(args, server)
    if args.cmd == 'hide':
        return _set_artist(args, server, 0, 'skrytý z adresáře')
    return _set_artist(args, server, 1, 'tatér')


if __name__ == '__main__':
    sys.exit(main())

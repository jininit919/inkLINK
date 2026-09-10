#!/usr/bin/env python3
"""Příprava na App Review u Mety.

Dvě věci, které se dělají jednou a ručně by to bylo klikání:

  reviewer   — založí testovací účet tatéra a vypíše přihlašovací údaje,
               které se posílají Metě. Heslo se generuje, ne vymýšlí.
  purge-ig   — smaže práce naimportované z Instagramu (a jen je), ať se
               dá import natočit načisto.

Použití (v Railway konzoli služby INKLINK):
    cd /app && /opt/venv/bin/python scripts/review_setup.py reviewer
    cd /app && /opt/venv/bin/python scripts/review_setup.py reviewer --apply
    cd /app && /opt/venv/bin/python scripts/review_setup.py purge-ig --user mgart
    cd /app && /opt/venv/bin/python scripts/review_setup.py purge-ig --user mgart --apply

Bez `--apply` se jen vypíše, co by se stalo. Mazání je nevratné, takže
nanečisto je výchozí stav schválně.
"""
import argparse
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REVIEWER_USERNAME = 'meta_review'
REVIEWER_EMAIL    = 'review@inklink.club'
REVIEWER_NAME     = 'Meta Review'
REVIEWER_CITY     = 'Praha'


def gen_password():
    # Bez znaků, které se v e-mailu Metě špatně přepisují (l/I/0/O).
    abc = ''.join(c for c in string.ascii_letters + string.digits if c not in 'lI0O')
    return ''.join(secrets.choice(abc) for _ in range(16))


def cmd_reviewer(args, server):
    conn = server.get_db()
    row = conn.execute('SELECT id, username, is_artist FROM users WHERE username=?',
                       (REVIEWER_USERNAME,)).fetchone()
    pwd = gen_password()

    if row:
        print(f'Účet @{REVIEWER_USERNAME} už existuje (#{row["id"]}).')
        print('Nastavím mu nové heslo — staré se stejně nikam nezapsalo.')
    else:
        print(f'Založím účet @{REVIEWER_USERNAME}.')

    if not args.apply:
        conn.close()
        print('\nNic se nezapsalo. Pro zápis spusť znovu s --apply.')
        return 0

    # Stejná metoda jako registrace (server.py), ať se hash liší jen daty.
    pwd_hash = server.generate_password_hash(pwd, method='pbkdf2:sha256')
    if row:
        conn.execute('UPDATE users SET password_hash=?, is_artist=1 WHERE id=?',
                     (pwd_hash, row['id']))
        uid = row['id']
    else:
        conn.execute(
            'INSERT INTO users (username, display_name, email, password_hash, city, '
            'is_artist, artist_slug, artist_terms_accepted_at) '
            'VALUES (?,?,?,?,?,1,?,?)',
            (REVIEWER_USERNAME, REVIEWER_NAME, REVIEWER_EMAIL, pwd_hash,
             REVIEWER_CITY, REVIEWER_USERNAME,
             server.datetime.utcnow().isoformat() + 'Z'))
        conn.commit()
        uid = conn.execute('SELECT id FROM users WHERE username=?',
                           (REVIEWER_USERNAME,)).fetchone()['id']
    conn.commit()
    conn.close()

    print(f'\nHotovo (#{uid}). Tohle pošli Metě do žádosti:\n')
    print(f'    e-mail:  {REVIEWER_EMAIL}')
    print(f'    heslo:   {pwd}')
    print('\nHeslo se nikam neukládá v čitelné podobě — když ho ztratíš,')
    print('spusť příkaz znovu a vygeneruje se nové.')
    return 0


def cmd_purge_ig(args, server):
    conn = server.get_db()
    user = conn.execute('SELECT id, username FROM users WHERE username=?',
                        (args.user,)).fetchone()
    if not user:
        print(f'Uživatel @{args.user} neexistuje.')
        conn.close()
        return 1

    # Jen práce, které opravdu přišly z Instagramu — podle záznamu o importu.
    # Ručně nahrané fotky se nesmí smazat spolu s nimi.
    rows = conn.execute(
        'SELECT i.id AS imp_id, i.portfolio_item_id AS item_id, i.ig_media_id, '
        '       p.caption, p.kind '
        'FROM instagram_imports i '
        'LEFT JOIN portfolio_items p ON p.id = i.portfolio_item_id '
        'WHERE i.user_id = ? ORDER BY i.id', (user['id'],)).fetchall()

    if not rows:
        print(f'@{user["username"]} nemá žádnou práci z Instagramu.')
        conn.close()
        return 0

    print(f'{"ZKOUŠKA (nic se nesmaže)" if not args.apply else "MAZÁNÍ"} — '
          f'@{user["username"]}, {len(rows)} položek z Instagramu\n')
    for r in rows:
        if r['item_id'] is None:
            print(f'  IG {r["ig_media_id"]:<22} — práce už neexistuje, zbyl jen záznam')
        else:
            cap = (r['caption'] or '')[:40]
            print(f'  #{r["item_id"]:<5} {r["kind"]:<7} {cap}')

    if not args.apply:
        conn.close()
        print('\nNic se nesmazalo. Pro smazání spusť znovu s --apply.')
        return 0

    for r in rows:
        if r['item_id'] is not None:
            conn.execute('DELETE FROM portfolio_likes WHERE item_id=?', (r['item_id'],))
            conn.execute('DELETE FROM portfolio_item_sizes WHERE item_id=?', (r['item_id'],))
            conn.execute('DELETE FROM portfolio_items WHERE id=?', (r['item_id'],))
        # Záznam o importu musí pryč taky, jinak se fotka v pickeru dál
        # tváří jako „už naimportováno" a nešla by vzít znovu.
        conn.execute('DELETE FROM instagram_imports WHERE id=?', (r['imp_id'],))
    conn.commit()
    conn.close()
    print(f'\nSmazáno {len(rows)} položek. Fotky jdou z Instagramu vzít znovu.')
    print('Soubory v úložišti zůstávají — mazání souborů dělá až úklid účtu.')
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    r = sub.add_parser('reviewer', help='testovací účet pro Metu')
    r.add_argument('--apply', action='store_true')

    p = sub.add_parser('purge-ig', help='smazat práce naimportované z Instagramu')
    p.add_argument('--user', required=True, help='uživatelské jméno tatéra')
    p.add_argument('--apply', action='store_true')

    args = ap.parse_args()
    import server
    return cmd_reviewer(args, server) if args.cmd == 'reviewer' else cmd_purge_ig(args, server)


if __name__ == '__main__':
    sys.exit(main())

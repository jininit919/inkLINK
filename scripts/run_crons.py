#!/usr/bin/env python3
"""Spouštěč cron jobů pro Railway.

Původně to byl `curl` v Custom Start Command. Jenže image postavená
Nixpacksem z Python repa curl neobsahuje — každý běh skončil na
`curl: command not found`. Python tam je z definice, tak voláme přes něj.

Použití (v Railway jako Custom Start Command):
    python scripts/run_crons.py booking-reminders
    python scripts/run_crons.py reconcile aftercare welcome-emails

Jeden spadlý job nesmí zastavit ostatní — projdou se všechny a nenulový
návratový kód se vrátí až na konci, aby Railway běh označil za neúspěšný.
"""
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get('CRON_BASE_URL', 'https://www.inklink.club').rstrip('/')
TIMEOUT_S = 300          # account-deletions i payouts můžou chvíli trvat


def run_job(job, token):
    req = urllib.request.Request(
        f'{BASE}/api/cron/{job}',
        headers={'X-Cron-Token': token, 'User-Agent': 'inklink-cron'},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        # Odpověď se vypisuje schválně: je v ní purged_count, paid, failed…
        # Bez ní by log říkal jen „OK" a nikdo by nevěděl, co job udělal.
        return resp.status, resp.read().decode('utf-8', 'replace')[:1000]


def main(jobs):
    if not jobs:
        print('Zadej aspoň jeden job, např. reconcile', file=sys.stderr)
        return 2
    token = os.environ.get('RECONCILE_TOKEN', '')
    if not token:
        # Bez tokenu vrátí server 403 na všechno — lepší to říct rovnou
        # než pětkrát opakovat tutéž chybu.
        print('RECONCILE_TOKEN není nastavený v této službě', file=sys.stderr)
        return 1

    failed = 0
    for job in jobs:
        try:
            status, body = run_job(job, token)
            print(f'{job} OK {status} {body}', flush=True)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:300]
            print(f'{job} FAILED HTTP {e.code} {detail}', flush=True)
            failed = 1
        except Exception as e:                    # síť, timeout, DNS…
            print(f'{job} FAILED {type(e).__name__}: {e}', flush=True)
            failed = 1
    return failed


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

# InkLink

Platforma pro tatéry: portfolio, kalendář volných termínů, online rezervace se zálohou přes Stripe Connect.

## Lokální start

```bash
cp .env.example .env   # vyplň klíče
pip install -r requirements.txt
./start.sh             # http://localhost:5001
```

SQLite DB se vytvoří v `inklink.db`. V produkci použij `DATABASE_URL` (Postgres).

## Stack

- Flask + Postgres (SQLite v dev)
- Stripe Connect Express (destination charges + application fee)
- Resend (mail)
- Cloudflare R2 (uploads, volitelně)
- Web Push notifikace

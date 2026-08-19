# MyM Shop — Stock Control

A simple web app for managing clothing shop inventory: items with photos, sizes,
colors, cost/sell price, quantity, stock status, best sellers, sales, advertising
spend, and profit/loss reporting.

## Run it locally

```bash
./run.sh
```

Then open http://127.0.0.1:5050 in your browser.

By default this uses a local SQLite database at `instance/stock.db` and stores
photos in `static/uploads/` — no setup needed. This mode is not suitable for a
production deployment: files don't survive a redeploy on most hosts.

## Stop it

Press `Ctrl+C` in the terminal running `run.sh`.

## Change the currency symbol

Edit the `CURRENCY` value near the top of `app.py`.

## Deploying to production (Render)

The app automatically switches to durable storage when these environment
variables are set (copy `.env.example` to `.env` for local testing, or set
them in Render's dashboard):

- `DATABASE_URL` — a Postgres connection string (e.g. from a free [Supabase](https://supabase.com) project)
- `CLOUDINARY_URL` — from a free [Cloudinary](https://cloudinary.com) account, for photo storage
- `SECRET_KEY` — any random string

### Steps

1. **Supabase**: create a free project, then copy the connection string from
   Project Settings → Database → Connection string (use the "URI" format,
   and adjust the scheme to `postgresql+psycopg://...`).
2. **Cloudinary**: create a free account, copy the `CLOUDINARY_URL` value
   from the Dashboard's "API Environment variable" box.
3. **Render**: create a free Web Service pointed at this project's repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app` (already declared in `Procfile`)
   - Add the three environment variables above in Render's dashboard.
4. Once deployed, point your domain (`mshop168.com`) at Render by adding the
   DNS records Render gives you under Settings → Custom Domain.

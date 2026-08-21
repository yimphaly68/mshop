# M Shop — Stock Control

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

## Production deployment

**Live at:** https://mshop168.com

Runs on a Hostinger VPS (`187.52.121.197`) under Docker Swarm, behind Traefik
for automatic HTTPS (Let's Encrypt). Data (SQLite database + uploaded photos)
lives on the VPS's persistent disk at `/opt/apps/mshop/data/`, so no external
database or photo host is needed — the app's built-in local storage is used
directly in production.

### Deploying an update

```bash
# 1. On your Mac, commit and push as usual
git add -A
git commit -m "describe your change"
git push origin main

# 2. On the VPS, pull + rebuild + roll out (zero downtime)
ssh -i ~/.ssh/mshop_vps root@187.52.121.197 /opt/apps/mshop/redeploy.sh
```

### Useful commands on the VPS

```bash
# View live logs
ssh -i ~/.ssh/mshop_vps root@187.52.121.197 "docker service logs -f mshop-app"

# Check service status
ssh -i ~/.ssh/mshop_vps root@187.52.121.197 "docker service ps mshop-app"
```

### Alternative: deploying elsewhere (e.g. Render)

The app also supports a Postgres + Cloudinary backend for hosts without
persistent disk (like Render's free tier), via environment variables — see
`.env.example`. Set `DATABASE_URL` (Postgres) and `CLOUDINARY_URL` (photo
storage) and the app automatically switches over; otherwise it uses local
SQLite/file storage as it does on the VPS.

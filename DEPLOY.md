# Deploy — VPS @ checkout.3dpresence.com

All secrets live in `.env` (never committed). Ubuntu 22.04/24.04 VPS assumed.

## 0. DNS (do this first, in your DNS provider)

Add an **A record**: `checkout.3dpresence.com` → `<your VPS public IP>`.
(Let it propagate before the TLS step — `dig +short checkout.3dpresence.com` should return the IP.)

## 1. Clone on the VPS (repo is public — no token needed)

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/happytimecustomerexperience-droid/budtender-pos.git
cd /opt/budtender-pos
```

## 2. Put your `.env` on the VPS (carries all real creds)

From your **Windows** machine (PowerShell), copy the local `.env` up:

```powershell
scp "C:\Users\vladi\OneDrive\Desktop\budtender-pos\.env" root@<VPS_IP>:/opt/budtender-pos/.env
```

It already has all 3 stores + `BUDTENDER_ALLOWED_HOSTS`/`BUDTENDER_CSRF_ORIGINS` =
`checkout.3dpresence.com` and `BUDTENDER_DEBUG=0`.

## 3. One-shot setup

```bash
cd /opt/budtender-pos
# optional: auto-create the budtender admin login in the same go
sudo DJANGO_SUPERUSER_USERNAME=admin \
     DJANGO_SUPERUSER_PASSWORD='choose-a-strong-one' \
     DJANGO_SUPERUSER_EMAIL=admin@3dpresence.com \
     CERTBOT_EMAIL=admin@3dpresence.com \
     bash deploy/bootstrap.sh checkout.3dpresence.com
```

This installs deps, runs migrations + collectstatic, creates the gunicorn **systemd**
service (`budtender`), the **nginx** reverse proxy, and **Let's Encrypt TLS**. When it
finishes: **https://checkout.3dpresence.com**.

If `certbot` ran before DNS resolved, re-run after it does:
`sudo certbot --nginx -d checkout.3dpresence.com`.

## 4. Verify

```bash
systemctl status budtender --no-pager
curl -I https://checkout.3dpresence.com            # 302 -> /login/ (good)
# Dutchie login works for all 3 stores:
sudo -u www-data /opt/budtender-pos/.venv/bin/python manage.py discover_registers
```

Open `https://checkout.3dpresence.com`, sign in, search a customer by phone → add → submit.

## 5. (optional) keep the browse cache warm

```bash
sudo crontab -e
# every 30 min:
*/30 * * * * cd /opt/budtender-pos && .venv/bin/python manage.py refresh_inventory >> /var/log/budtender-inv.log 2>&1
```

## Updating later

```bash
cd /opt/budtender-pos && sudo git pull
sudo .venv/bin/pip install -e ".[prod]"
sudo .venv/bin/python manage.py migrate --noinput
sudo .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart budtender
```

## Notes
- App listens on `127.0.0.1:8000` (gunicorn, gthread); nginx terminates TLS and proxies.
- `SECURE_PROXY_SSL_HEADER` is set, so Django sees HTTPS behind nginx; `BUDTENDER_DEBUG=0`
  turns on SSL-redirect + secure cookies + HSTS.
- WhiteNoise serves static; uploads capped at 12 MB (id-scan images).
- Customer-360 history stays "unavailable" until you set `DASHBOARD_DB_DSN` +
  `DASHBOARD_TENANT_SCHEMA` in `.env` (read-only Postgres of the dashboard).

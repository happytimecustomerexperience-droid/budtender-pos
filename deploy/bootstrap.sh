#!/usr/bin/env bash
# One-shot VPS setup for Budtender POS (Ubuntu 22.04/24.04).
# Run from inside the cloned repo, as root, after placing .env:
#     sudo bash deploy/bootstrap.sh checkout.3dpresence.com
# Idempotent-ish: safe to re-run. Optional superuser via env:
#     DJANGO_SUPERUSER_USERNAME=admin DJANGO_SUPERUSER_PASSWORD=... DJANGO_SUPERUSER_EMAIL=a@b.c sudo -E bash deploy/bootstrap.sh <domain>
set -euo pipefail

DOMAIN="${1:-checkout.3dpresence.com}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP_DIR/.venv/bin/python"
SVC_USER="${SVC_USER:-www-data}"

echo ">> domain=$DOMAIN  app_dir=$APP_DIR  user=$SVC_USER"
[ -f "$APP_DIR/.env" ] || { echo "!! $APP_DIR/.env missing — scp it first (it holds all secrets)"; exit 1; }

echo ">> [1/8] system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip git nginx curl

echo ">> [2/8] virtualenv + deps"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$PY" -m pip install -U pip wheel
"$PY" -m pip install -e "$APP_DIR[prod]"

echo ">> [3/8] migrate + collectstatic"
cd "$APP_DIR"
"$PY" manage.py migrate --noinput
"$PY" manage.py collectstatic --noinput

echo ">> [4/8] optional superuser"
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  "$PY" manage.py createsuperuser --noinput || echo "   (superuser exists, skipping)"
else
  echo "   set DJANGO_SUPERUSER_* env to auto-create, or run: $PY manage.py createsuperuser"
fi

echo ">> [5/8] permissions"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"

echo ">> [6/8] systemd service"
cat > /etc/systemd/system/budtender.service <<EOF
[Unit]
Description=Budtender POS (gunicorn)
After=network.target

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=BUDTENDER_BIND=127.0.0.1:8000
ExecStart=$APP_DIR/.venv/bin/gunicorn -c $APP_DIR/gunicorn.conf.py budtender_pos.wsgi
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now budtender
systemctl restart budtender

echo ">> [7/8] nginx reverse proxy"
cat > /etc/nginx/sites-available/budtender <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 12m;            # id-scan image uploads
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/budtender /etc/nginx/sites-enabled/budtender
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx
nginx -t && (systemctl restart nginx || { echo "   nginx restart failed — is port 80 taken (docker)?"; ss -ltnp | grep ':80 ' || true; })

echo ">> [8/8] TLS (Let's Encrypt)"
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect \
  -m "${CERTBOT_EMAIL:-admin@3dpresence.com}" || \
  echo "   certbot failed (DNS not pointed yet?). After DNS resolves, run: certbot --nginx -d $DOMAIN"

echo ">> done. https://$DOMAIN  (service: systemctl status budtender)"

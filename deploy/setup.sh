#!/bin/bash
# AYONIME VPS Setup — Ubuntu 22.04, 4 cores / 8GB RAM
# Usage: bash setup.sh yourdomain.com
set -e

DOMAIN=${1:?"Usage: bash setup.sh yourdomain.com"}
APP_DIR="/var/www/ayonime"
REPO_FRONTEND="https://github.com/AyoFemi10/ayonime.git"
REPO_BACKEND="https://github.com/AyoFemi10/animepahe-dl.git"

echo "━━━ [1/8] System packages ━━━"
sudo apt-get update -y
sudo apt-get install -y \
    nginx ffmpeg nodejs npm \
    python3 python3-pip python3-venv \
    git certbot python3-certbot-nginx \
    htop ufw

echo "━━━ [2/8] Firewall ━━━"
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable

echo "━━━ [3/8] Clone repos ━━━"
sudo mkdir -p $APP_DIR
sudo chown $USER:$USER $APP_DIR

# Frontend
git clone $REPO_FRONTEND $APP_DIR/frontend

# Backend (animepahe-dl — the Python downloader)
git clone $REPO_BACKEND $APP_DIR/backend_src

echo "━━━ [4/8] Python backend ━━━"
python3 -m venv $APP_DIR/venv
$APP_DIR/venv/bin/pip install --upgrade pip -q
$APP_DIR/venv/bin/pip install -q -r $APP_DIR/backend_src/requirements.txt
$APP_DIR/venv/bin/pip install -q fastapi "uvicorn[standard]"

# Copy backend entrypoint into backend_src so imports resolve
cp -r $APP_DIR/frontend/../backend/main.py $APP_DIR/backend_src/backend_main.py 2>/dev/null || true

echo "━━━ [5/8] Next.js build ━━━"
cd $APP_DIR/frontend
npm ci --silent
NEXT_PUBLIC_API_URL="https://$DOMAIN" npm run build

echo "━━━ [6/8] Nginx ━━━"
sudo cp $APP_DIR/frontend/deploy/nginx.conf /etc/nginx/nginx.conf
sudo sed -i "s/yourdomain.com/$DOMAIN/g" /etc/nginx/nginx.conf
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx

echo "━━━ [7/8] SSL certificate ━━━"
sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN \
    --non-interactive --agree-tos -m "admin@$DOMAIN" --redirect

echo "━━━ [8/8] Systemd services ━━━"
# Backend service
sudo tee /etc/systemd/system/ayonime-backend.service > /dev/null <<EOF
[Unit]
Description=AYONIME FastAPI Backend
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR/backend_src
ExecStart=$APP_DIR/venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 4
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=ALLOWED_ORIGINS=https://$DOMAIN

[Install]
WantedBy=multi-user.target
EOF

# Frontend service
sudo tee /etc/systemd/system/ayonime-frontend.service > /dev/null <<EOF
[Unit]
Description=AYONIME Next.js Frontend
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR/frontend
ExecStart=$(which node) node_modules/.bin/next start -p 3000
Restart=on-failure
RestartSec=5
Environment=NODE_ENV=production
Environment=NEXT_PUBLIC_API_URL=https://$DOMAIN
Environment=UV_THREADPOOL_SIZE=4

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ayonime-backend ayonime-frontend
sudo systemctl start ayonime-backend ayonime-frontend

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AYONIME is live at https://$DOMAIN"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Useful commands:"
echo "  systemctl status ayonime-backend"
echo "  systemctl status ayonime-frontend"
echo "  journalctl -u ayonime-backend -f      # backend logs"
echo "  journalctl -u ayonime-frontend -f     # frontend logs"
echo ""
echo "To redeploy after code changes:"
echo "  cd $APP_DIR/frontend && git pull && npm run build && sudo systemctl restart ayonime-frontend"

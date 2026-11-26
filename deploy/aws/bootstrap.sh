#!/usr/bin/env bash
# Runs once as root on a fresh Amazon Linux 2023 instance (pass as EC2 user-data).
# Installs Docker, starts Postgres 16 (or uses your own: set PG_DSN), builds the API from GitHub, and puts
# Caddy in front for HTTPS.  Env set by the launcher: REPO_URL, CORS_ORIGINS, SPA_NAME, optional PG_DSN.
set -euxo pipefail
dnf install -y docker git
systemctl enable --now docker

mkdir -p /opt/spa /var/lib/spa/backups
git clone --depth 1 "${REPO_URL:-https://github.com/ashfaq11/management-backend}" /opt/spa/src
docker network create spa 2>/dev/null || true

# Secrets are generated on the box and never leave it; read them with `sudo cat /opt/spa/env`.
if [ ! -f /opt/spa/env ]; then
  umask 077
  rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
  PGPW=$(rand 24)
  cat > /opt/spa/env <<EOF
ADMIN_USER=admin
ADMIN_PASSWORD=$(rand 20)
SPA_NAME=${SPA_NAME:-Serenity Spa}
SPA_CORS_ORIGINS=${CORS_ORIGINS:-http://localhost:4200}
PG_DSN=${PG_DSN:-postgresql://spa:${PGPW}@spa-pg:5432/spa}
EOF
  echo "$PGPW" > /opt/spa/pg-password
fi

# Local Postgres, unless an external database was given. Data lives in the docker volume spa-pgdata.
if [ -z "${PG_DSN:-}" ]; then
  docker rm -f spa-pg 2>/dev/null || true
  docker run -d --name spa-pg --network spa --restart unless-stopped \
    -e POSTGRES_USER=spa -e POSTGRES_DB=spa -e POSTGRES_PASSWORD="$(cat /opt/spa/pg-password)" \
    -v spa-pgdata:/var/lib/postgresql/data postgres:16
  for _ in $(seq 1 30); do docker exec spa-pg pg_isready -U spa -d spa && break; sleep 2; done
fi

docker build -t spa-api /opt/spa/src
docker rm -f spa-api 2>/dev/null || true
# The API applies its own migrations on start (schema "spa").
docker run -d --name spa-api --network spa --restart unless-stopped --env-file /opt/spa/env \
  -p 127.0.0.1:8080:8080 spa-api

# HTTPS: <public-ip>.sslip.io resolves to the IP and Caddy gets a free Let's Encrypt cert for it.
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
mkdir -p /opt/spa/caddy
cat > /opt/spa/caddy/Caddyfile <<EOF
${IP}.sslip.io {
  reverse_proxy 127.0.0.1:8080
}
EOF
docker rm -f spa-caddy 2>/dev/null || true
docker run -d --name spa-caddy --restart unless-stopped --network host \
  -v /opt/spa/caddy/Caddyfile:/etc/caddy/Caddyfile:ro -v /opt/spa/caddy/data:/data caddy:2

# nightly backup at 02:30
echo "30 2 * * * root /opt/spa/src/deploy/aws/backup.sh >> /var/log/spa-backup.log 2>&1" > /etc/cron.d/spa-backup
echo "API: https://${IP}.sslip.io/api/health" > /opt/spa/URL

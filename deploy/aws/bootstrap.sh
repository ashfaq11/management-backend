#!/usr/bin/env bash
# Runs once as root on a fresh Amazon Linux 2023 instance (pass as EC2 user-data).
# Installs Docker, builds the API from GitHub, and puts Caddy in front for HTTPS.
# Env (set by the launcher): REPO_URL, CORS_ORIGINS, SPA_NAME
set -euxo pipefail
dnf install -y docker git
systemctl enable --now docker

mkdir -p /opt/spa /var/lib/spa
chown 10001 /var/lib/spa
git clone --depth 1 "${REPO_URL:-https://github.com/ashfaq11/management-backend}" /opt/spa/src

# Secrets are generated on the box and never leave it; read them with `sudo cat /opt/spa/env`.
if [ ! -f /opt/spa/env ]; then
  umask 077
  cat > /opt/spa/env <<EOF
ADMIN_USER=admin
ADMIN_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
SPA_NAME=${SPA_NAME:-Serenity Spa}
SPA_CORS_ORIGINS=${CORS_ORIGINS:-http://localhost:4200}
EOF
fi

docker build -t spa-api /opt/spa/src
docker rm -f spa-api 2>/dev/null || true
docker run -d --name spa-api --restart unless-stopped --env-file /opt/spa/env \
  -v /var/lib/spa:/data -p 127.0.0.1:8080:8080 spa-api

# HTTPS: <public-ip>.sslip.io resolves to the IP and Caddy gets a free Let's Encrypt cert for it.
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
HOST="${IP//./-}.sslip.io"
mkdir -p /opt/spa/caddy
cat > /opt/spa/caddy/Caddyfile <<EOF
${IP}.sslip.io {
  reverse_proxy 127.0.0.1:8080
}
EOF
docker rm -f spa-caddy 2>/dev/null || true
docker run -d --name spa-caddy --restart unless-stopped --network host \
  -v /opt/spa/caddy/Caddyfile:/etc/caddy/Caddyfile:ro -v /opt/spa/caddy/data:/data caddy:2
echo "API: https://${IP}.sslip.io/api/health" > /opt/spa/URL

#!/usr/bin/env bash
# Redeploy on the server: sudo bash /opt/spa/src/deploy/aws/update.sh
# New migrations in migrations/ are applied automatically when the API starts.
set -euxo pipefail
cd /opt/spa/src && git pull --ff-only
docker build -t spa-api .
docker rm -f spa-api
docker run -d --name spa-api --network spa --restart unless-stopped --env-file /opt/spa/env \
  -p 127.0.0.1:8080:8080 spa-api
sleep 5 && curl -fs http://127.0.0.1:8080/api/health

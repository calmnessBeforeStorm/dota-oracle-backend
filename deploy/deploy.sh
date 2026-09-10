#!/usr/bin/env bash
# Pull the published images, run migrations, bring the stack up.
#
#     ./deploy.sh                       # whatever .env says, normally main for both
#     ./deploy.sh <api-sha> <spa-sha>   # roll back to one exact pair
#
# Every successful deploy appends its pair to deployed.log, which is where the two SHAs for
# a rollback come from.
#
# Images are built by GitHub Actions on push to `main` and pulled from ghcr; nothing is built
# here.
#
# A rollback takes two SHAs, not one, and that is not an oversight: the API and the SPA are
# separate repositories with separate commit histories, so no single number names a matching
# pair. Following `main` keeps them in step day to day; naming one and letting the other
# drift is exactly the failure the single image used to prevent, so this refuses a single
# argument rather than guessing what the other half should be.
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILE=docker-compose.prod.yml
ENV_FILE=.env

# Written only after the public URL answers, so the log is a list of deploys that actually
# worked - which is the list a rollback needs.
#
# It exists because a rollback names two SHAs and no single number produces them: the API
# and the SPA are separate repositories with separate histories. Without this you would be
# reconstructing which pair was live by reading two commit logs against a clock, at the one
# moment when that is hardest.
DEPLOY_LOG=deployed.log

record_deploy() {
    # Asked of compose rather than of the arguments, so the log records what was actually
    # resolved - including the run where .env supplied the tag and nothing was passed in.
    images=$(docker compose -f "$COMPOSE_FILE" config --images)
    api_image=$(echo "$images" | grep dota-oracle-api | head -1)
    spa_image=$(echo "$images" | grep dota-oracle-frontend | head -1)
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  api=${api_image##*:}  frontend=${spa_image##*:}" \
        >> "$DEPLOY_LOG"
    echo "deploy: recorded in $DEPLOY_LOG"
}

fail() {
    echo "deploy: $*" >&2
    exit 1
}

[ -f "$ENV_FILE" ] || fail "$ENV_FILE is missing - copy deploy/env.prod.example and fill it in"
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE is missing"

# The file holds the database password and every API key. World-readable is a finding, not a
# style preference, and it is easy to reintroduce with an editor that rewrites the file.
perms=$(stat -c '%a' "$ENV_FILE")
[ "$perms" = "600" ] || fail "$ENV_FILE is mode $perms, expected 600 - run: chmod 600 $ENV_FILE"

# Serving a model requires the artifact and its card. Without them the predictor falls back
# to the baseline *silently* - the log says so, but a page nobody is watching does not - so
# this refuses to deploy rather than quietly serving worse numbers under the same version.
active=$(grep -E '^ACTIVE_MODEL_VERSION=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
if [ -n "$active" ]; then
    for suffix in .txt .card.json; do
        [ -f "models/$active$suffix" ] || fail "models/$active$suffix is missing (ACTIVE_MODEL_VERSION=$active)"
    done
fi

case $# in
    0) ;;
    2)
        export API_TAG="$1" FRONTEND_TAG="$2"
        echo "deploy: api $API_TAG, frontend $FRONTEND_TAG (overriding .env)"
        ;;
    *) fail "expected either no arguments or both tags: ./deploy.sh <api-sha> <spa-sha>" ;;
esac

echo "deploy: pulling"
docker compose -f "$COMPOSE_FILE" pull

# Migrations run before the new code serves anything, in a one-shot container off the same
# image the API will run. `run --rm` and not `exec`, because at this point the new API is not
# up yet and the old one must not be the thing applying the new schema.
echo "deploy: migrating"
docker compose -f "$COMPOSE_FILE" up -d postgres redis
docker compose -f "$COMPOSE_FILE" run --rm --no-deps api alembic upgrade head

echo "deploy: starting"
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# Old image layers pile up fast with a tag per commit. Only dangling ones: a pruned image
# that is still referenced would force a re-pull on the next restart.
docker image prune -f >/dev/null

# Two checks, because they fail for different reasons and one message for both would send
# you to the wrong place.
#
# Not `curl http://localhost/api/health`: Caddy redirects http to https and answers only for
# the configured domain, so localhost over plain http is a 308 to a name it does not serve -
# a health check that can never pass.
echo "deploy: waiting for the API"
for _ in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" exec -T api curl -fsS -o /dev/null http://localhost:8000/api/health; then
        api_up=yes
        break
    fi
    sleep 2
done

if [ "${api_up:-no}" != "yes" ]; then
    echo "deploy: the API did not become healthy in 60s" >&2
    docker compose -f "$COMPOSE_FILE" ps
    docker compose -f "$COMPOSE_FILE" logs --tail 50 api
    exit 1
fi

# The public path: DNS, Caddy, the certificate. Its failures are not the API's, and on a
# first deploy the usual one is the orange cloud in Cloudflare - behind it the ACME challenge
# never reaches Caddy and no certificate is issued.
domain=$(grep -E '^DOMAIN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
echo "deploy: waiting for https://$domain"
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "https://$domain/api/health"; then
        record_deploy
        echo "deploy: ok"
        docker compose -f "$COMPOSE_FILE" ps
        exit 0
    fi
    sleep 2
done

echo "deploy: the API is healthy but https://$domain is not answering." >&2
echo "        Check the A records are DNS only (grey cloud) and SSL/TLS is Full (strict)." >&2
docker compose -f "$COMPOSE_FILE" logs --tail 50 caddy
exit 1

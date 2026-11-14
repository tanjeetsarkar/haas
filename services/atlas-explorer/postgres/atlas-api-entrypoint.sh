#!/bin/sh
set -eu

if [ -n "${ATLAS_DATABASE_URL_FILE:-}" ]; then
    ATLAS_DATABASE_URL="$(cat "$ATLAS_DATABASE_URL_FILE")"
else
    password="$(cat "$ATLAS_RUNTIME_PASSWORD_FILE")"
    ATLAS_DATABASE_URL="postgresql://${ATLAS_DATABASE_USER}:${password}@${ATLAS_DATABASE_HOST}:${ATLAS_DATABASE_PORT}/${ATLAS_DATABASE_NAME}"
    unset password
fi
export ATLAS_DATABASE_URL
exec "$@"

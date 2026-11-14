#!/bin/sh
set -eu

umask 077
mkdir -p postgres/secrets
chmod 700 postgres/secrets
for name in bootstrap_password migration_password runtime_password; do
    path="postgres/secrets/$name"
    if [ -e "$path" ]; then
        printf '%s\n' "Refusing to overwrite existing secret file: $path" >&2
        exit 1
    fi
    openssl rand -hex 32 > "$path"
    chmod 644 "$path"
done
printf '%s\n' "Created Atlas role-password files in the private postgres/secrets/ directory (values not displayed)."

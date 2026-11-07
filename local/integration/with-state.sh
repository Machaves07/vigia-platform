#!/bin/sh
# Entrada de los servicios de la plataforma (TASK-545): lee del volumen lo que no debe ir en un
# archivo de composición ni en `docker inspect` y exec la orden. Cada lectura es opcional: un
# servicio solo recibe lo que su volumen le monta (`vigia-api` y `vigia-worker` no montan `estado`).
set -eu
if [ -r /state/app.pw ]; then
    VIGIA_DB_APP_PASSWORD="$(cat /state/app.pw)"
    VIGIA_DB_MIGRATE_PASSWORD="$(cat /state/migrate.pw)"
    export VIGIA_DB_APP_PASSWORD VIGIA_DB_MIGRATE_PASSWORD
fi
if [ -s /shared/provider_organization_id ]; then
    VIGIA_PROVIDER_ORGANIZATION_ID="$(cat /shared/provider_organization_id)"
    export VIGIA_PROVIDER_ORGANIZATION_ID
fi
exec "$@"

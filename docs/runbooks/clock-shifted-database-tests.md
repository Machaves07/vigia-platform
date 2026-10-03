# Runbook · Probar el código sensible al reloj con la base en otra fecha

**Unidad**: U-02 · **Tarea**: TASK-152 (VIG-97), por el seguimiento de la revisión independiente de VIG-135 (PR #55) · **Regla**: retro 15 de `AGENTS.md` del espacio de trabajo («dos relojes en la misma prueba»).
**Ejecuta**: el agente o la persona que toca vigencias de claves, tokens, concesiones o cualquier comparación con `now()` de la base. Solo en local o en WSL, con datos generados.
**Índice**: [runbooks de operación](README.md).

## Cuándo

Cuando una tarea toca código que compara marcas de tiempo con el reloj de la base (`now()`, `CURRENT_TIMESTAMP`, `clock_timestamp()`) o deriva vigencias (`valid_until`, `notAfter`, caducidad de sesiones o concesiones). La regla pide evidencia con **la base y el proceso más de un año por delante**.

Usar `faketime` solo sobre pytest no basta: el reloj que importa es el `now()` de PostgreSQL, que corre en otro proceso. Hay que adelantar los dos.

## Pasos

1. **Imagen de PostgreSQL con libfaketime.** El mismo digest que `docker-compose.yml` y `backend/tests/integration/conftest.py` (`POSTGRES_IMAGE`), más el paquete. Fuera del repositorio, por ejemplo en `/tmp/<issue>/faketime/Dockerfile`:

   ```dockerfile
   FROM postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54
   RUN apt-get update \
    && apt-get install -y --no-install-recommends libfaketime \
    && rm -rf /var/lib/apt/lists/*
   ENV FAKETIME_DISABLE_SHM=1 FAKETIME_NO_CACHE=1
   ```

   ```text
   docker build -t vigia-postgres-faketime:16 /tmp/<issue>/faketime
   ```

   En `amd64` la biblioteca queda en `/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1`; en `arm64`, en `/usr/lib/aarch64-linux-gnu/faketime/`.
2. **Levantar la base adelantada**, con una **fecha absoluta** (`@`, el reloj avanza desde ahí) y en el puerto del entorno local, para que las pruebas la usen con `VIGIA_TEST_USE_COMPOSE=1`:

   ```text
   docker run -d --name vigia-faketime -p 127.0.0.1:5432:5432 \
     -e POSTGRES_USER=vigia -e POSTGRES_PASSWORD=vigia_local -e POSTGRES_DB=vigia -e TZ=UTC -e PGTZ=UTC \
     -e LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 \
     -e "FAKETIME=@2028-03-01 12:00:00" \
     vigia-postgres-faketime:16
   docker exec vigia-faketime psql -U vigia -d vigia -tAc "SELECT now(), clock_timestamp()"
   ```

   La consulta debe devolver 2028. Si devuelve la fecha real, la variable `LD_PRELOAD` no llegó al proceso.
3. **LocalStack**, si las pruebas lo necesitan: `docker compose up -d localstack`. No adelantes LocalStack salvo que la prueba compare con su reloj.
4. **Correr pytest con el mismo reloj**, la misma `LD_PRELOAD` (la ruta de la biblioteca en el sistema anfitrión: `apt-get install libfaketime` en WSL) y la misma `FAKETIME`, con `-n 3` como máximo:

   ```text
   cd backend
   export DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales      # WSL: ver AGENTS.md
   VIGIA_TEST_USE_COMPOSE=1 FAKETIME_DISABLE_SHM=1 FAKETIME_NO_CACHE=1 \
     LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 \
     FAKETIME="@2028-03-01 12:00:00" \
     uv run pytest -q -n 3 -m integration <archivos de la tarea>
   ```

5. **Pegar en `Evidence`** del PR la salida de la consulta del paso 2 (la fecha de la base) y la de pytest.
6. **Limpiar**: `docker rm -f vigia-faketime` y `docker compose down -v` si se levantó LocalStack. No dejes contenedores vivos.

## Validación posterior

- [ ] `SELECT now()` en la base devuelve la fecha adelantada (más de un año sobre la real).
- [ ] La corrida de pytest con `LD_PRELOAD` y `FAKETIME` pasa con las mismas pruebas que sin ellas, salvo los artefactos conocidos de la lista de abajo.
- [ ] No queda ningún contenedor `vigia-faketime` (`docker ps -a`).

**Artefactos conocidos.** Dos pruebas de `lock_timeout` fallaron con libfaketime en la revisión de VIG-135 y se deseleccionaron allí con `--deselect`:

- `tests/examples/test_ledger_routes.py::test_a_burst_waiting_for_the_users_lock_is_rate_limited`
- `tests/abuse/test_n09_live_view_token.py::test_n09_a_slow_issuance_holds_only_its_own_person`

Falta comprobar si pasan con la fecha absoluta (`@fecha`) de este runbook. La primera corrida que lo haga anota aquí el resultado.

## Comunicación

Sin comunicación externa: es un procedimiento de pruebas. El resultado va en el workpad de la tarea y en la sección `Evidence` del PR, con la fecha de la base y del proceso.

## Estado en el código

- El repositorio no tiene imagen de PostgreSQL con libfaketime ni ninguna prueba que la use. Las pruebas usan el reloj inyectado (`backend/tests/virtual_time.py`) y este runbook es la comprobación adicional que pide la regla.
- **Verificado en TASK-152**: el `Dockerfile` del paso 1 construye con el digest fijado (Debian 13) y deja `libfaketime.so.1` y `libfaketimeMT.so.1` en `/usr/lib/x86_64-linux-gnu/faketime/`. **No verificado**: los pasos 2 a 4 (base adelantada, pytest con `LD_PRELOAD`) ni los dos artefactos conocidos.

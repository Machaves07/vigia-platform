# Imagen de vigia-platform (U-02, TASK-143; LC-NUC-35; tech-stack-decisions.md §6; NFR-NUC-23, 24).
#
# Una imagen, dos procesos: `vigia-api` (orden por defecto) y `vigia-worker` (la orden de su
# servicio); las tareas puntuales `vigia-migrate` (`alembic upgrade head`) y `vigia-admin` usan la
# misma imagen. Sin etapa de la aplicación de página única: llega con U-05.
#
# Construcción (desde la raíz del repositorio; la clave de despliegue de vigia-contracts, de solo
# lectura, en el agente SSH del que construye):
#
#   docker buildx build --platform linux/arm64 --ssh default -f backend/Dockerfile .
#
# - Base y `uv` fijados por digest del índice multiplataforma; ninguna etiqueta móvil. Para
#   actualizar: `docker buildx imagetools inspect <imagen>:<etiqueta>` y copiar el `Digest`.
# - `vigia-contracts` se instala por `git+ssh` (ADR-004) con `RUN --mount=type=ssh`: la clave
#   nunca entra en una capa ni en el historial; solo el socket del agente existe durante esa orden
#   (nota de 2026-09-23 de tech-stack-decisions.md §6). `git` y `ssh` quedan en la etapa de
#   construcción.
# - Ejecución: usuario `vigia` (10001) sin privilegios, sin binarios setuid ni setgid, código
#   propiedad de root y de solo lectura, bytecode precompilado: la imagen arranca con la raíz de
#   solo lectura y `/tmp` montado aparte (infrastructure-design.md §5.3 y su nota de 2026-09-23:
#   el usuario sin privilegios sustituye a `no-new-privileges`, que Fargate no admite).

ARG PYTHON_IMAGE=python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424

FROM ${UV_IMAGE} AS uv

# --- Construcción ------------------------------------------------------------------------------

FROM ${PYTHON_IMAGE} AS build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

# Huella publicada de github.com (la misma que usa .github/workflows/ci.yml): sin ella, `ssh`
# rechaza el servidor; nunca se acepta una huella nueva sin comprobar.
RUN install -d -m 0755 /etc/ssh \
    && echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" \
       > /etc/ssh/ssh_known_hosts

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"

WORKDIR /app

# Dependencias primero (capa reutilizable mientras no cambie el bloqueo). Instalación congelada y
# sin el grupo `dev`; el agente SSH solo existe durante esta orden.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=ssh \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# El paquete se instala editable sobre /app/src: las rutas de los recursos se resuelven desde el
# árbol (`resources/`, `tools/vigia_verify.py`), igual que en el repositorio.
COPY backend/src ./src
COPY backend/resources ./resources
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY backend/tools/vigia_verify.py ./tools/vigia_verify.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && python -m compileall -q --invalidation-mode checked-hash src migrations tools

# --- Ejecución ---------------------------------------------------------------------------------

FROM ${PYTHON_IMAGE} AS runtime

# Arreglos de seguridad de Debian publicados después del digest de la base (el escaneo de `ci.yml`
# falla con una vulnerabilidad alta que ya tenga arreglo) y sin `pip`, que la imagen no usa: el
# entorno lo instala `uv` en la construcción.
#
# Bibliotecas nativas de WeasyPrint para el documento del acta (TASK-217, NFR-GOB-32): Pango
# (>= 1.44) con `pangoft2`, HarfBuzz con su subconjunto de fuentes y fontconfig. Sin fuentes del
# sistema: Debian las arrastra como dependencia de `fontconfig-config` y se purgan, así que el
# documento solo puede usar las fuentes empaquetadas en `backend/resources/fonts/` (SIL OFL 1.1).
# La purga con `--force-depends` deja `fontconfig-config` con su dependencia de fuentes sin cumplir
# en la base de dpkg. La ejecución no la nota y la imagen no usa apt, pero una imagen derivada que
# haga `apt-get install` fallará: tendría que reinstalar las fuentes o usar un paquete vacío que
# las provea (equivs).
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && apt-get install --yes --no-install-recommends \
       libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libfontconfig1 \
    && fonts="$(dpkg-query --show --showformat='${db:Status-Abbrev} ${Package}\n' 'fonts-*' 2>/dev/null \
       | awk '$1 == "ii" { print $2 }')" \
    && if [ -n "$fonts" ]; then dpkg --purge --force-depends $fonts; fi \
    && test -z "$(find /usr/share/fonts /usr/local/share/fonts -type f 2>/dev/null)" \
    && rm -rf /var/lib/apt/lists/* /var/cache/fontconfig \
    && python -m pip uninstall --yes --quiet pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

RUN groupadd --system --gid 10001 vigia \
    && useradd --system --uid 10001 --gid vigia --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin vigia \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} +

# Propiedad de root con los permisos de la construcción (umask 022): `vigia` lee y ejecuta, no
# escribe.
COPY --from=build --chown=root:root /app /app

# La caché de fontconfig (y las fuentes temporales de WeasyPrint) van a `/tmp`: la raíz sigue
# siendo de solo lectura.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/cache

WORKDIR /app
USER 10001:10001
EXPOSE 8000
CMD ["vigia-api"]

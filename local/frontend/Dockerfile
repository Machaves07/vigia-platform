# Servidor de desarrollo de la aplicación (VIG-288; infrastructure-design.md de U-05 §2,
# deployment-architecture.md §8): servicio `frontend` de `docker-compose.yml`, perfil `frontend`.
#
# La misma base que la etapa `frontend-build` de `backend/Dockerfile` (Node LTS 22, fijada por
# digest), con `git` y `ssh` para que `npm ci` instale `@vigia/contracts` por `git+ssh` (ADR-004)
# con el agente SSH de quien lo arranca; la clave nunca entra en la imagen. Para actualizar la
# base: `docker buildx imagetools inspect node:22-bookworm-slim` y copiar el `Digest` del índice
# (en los dos Dockerfile).

FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Huella publicada de github.com (la misma que `backend/Dockerfile` y `ci.yml`).
RUN install -d -m 0755 /etc/ssh \
    && echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" \
       > /etc/ssh/ssh_known_hosts

# `node_modules` es un volumen con nombre (no el del anfitrión, que puede ser de otro sistema): se
# crea aquí para que el volumen nazca con el propietario `node`.
RUN install -d -o node -g node /src/frontend/node_modules

ENV GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes" \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /src/frontend
USER node

EXPOSE 5173
CMD ["sh", "-c", "npm ci --ignore-scripts && exec node node_modules/vite/bin/vite.js --host 0.0.0.0"]

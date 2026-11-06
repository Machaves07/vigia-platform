# Balanceador del entorno de integración (TASK-542; A-76 D3): `vigia-mtls-edge`, dos escuchas TLS
# (`app.` y `nodes.`) delante de `vigia-api`. Nunca en producción, donde termina TLS el ALB (A-47).
#
# Contexto de construcción: `backend/tools` (solo lo que copia esta imagen):
#
#   docker build -f local/mtls-edge/Dockerfile -t vigia-local/mtls-edge:1.0.0 backend/tools
#
# La misma base que `backend/Dockerfile`, fijada por digest (la prueba
# `tests/unit/test_mtls_edge.py` exige que coincidan). Las dependencias, las de `uv.lock` por suma
# (`tools/requirements/mtls-edge.txt`); sin `vigia_platform` ni `vigia-contracts`: construirla no
# necesita clave SSH y `server-casa` no guarda ninguna.
#
# El certificado de servidor y su clave van en un volumen del usuario 10001, nunca en la imagen:
#
#   docker run ... vigia-local/mtls-edge:1.0.0 init-cert --san app.<zona> --san nodes.<zona> --ip <ip>

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

COPY requirements/mtls-edge.txt /tmp/mtls-edge.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --root-user-action=ignore -r /tmp/mtls-edge.txt \
    && rm /tmp/mtls-edge.txt

COPY __init__.py mtls_edge.py /opt/vigia/tools/

# La orden `vigia-mtls-edge` de `[project.scripts]`: aquí no se instala el proyecto, solo su módulo.
RUN printf '#!/bin/sh\nexec python -m tools.mtls_edge "$@"\n' > /usr/local/bin/vigia-mtls-edge \
    && chmod 0755 /usr/local/bin/vigia-mtls-edge

ENV PYTHONPATH=/opt/vigia \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001

EXPOSE 8443 8444
ENTRYPOINT ["vigia-mtls-edge"]
CMD ["serve"]

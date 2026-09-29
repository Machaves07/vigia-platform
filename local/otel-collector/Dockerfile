# Colector de OpenTelemetry del entorno local (TASK-103, LC-NUC-36).
#
# La imagen oficial `otel/opentelemetry-collector-contrib` (Apache-2.0) se construye desde
# `scratch`: solo trae el binario, sin shell ni sonda, así que Docker no puede comprobar su salud.
# Esta imagen copia ese mismo binario, sin cambios, sobre una base Debian mínima para que la
# comprobación de `docker-compose.yml` consulte la extensión `health_check` (puerto 13133).
# Las dos imágenes de origen están fijadas por digest. Para actualizarlas: `docker buildx
# imagetools inspect <imagen>:<etiqueta>` y copiar el `Digest` del índice.

FROM otel/opentelemetry-collector-contrib:0.161.0@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1 AS collector

FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

COPY --from=collector /otelcol-contrib /otelcol-contrib
COPY config.yaml /etc/otelcol-contrib/config.yaml

# Mismo usuario sin privilegios que la imagen oficial.
USER 10001:10001

ENTRYPOINT ["/otelcol-contrib"]
CMD ["--config=/etc/otelcol-contrib/config.yaml"]

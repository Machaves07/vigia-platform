# Precisión técnica de la sonda H.264 de arranque

Fecha: 2026-10-25 · VIG-343 (seguimiento de VIG-299)

R-APP-2, cerrado por la nota de 2026-09-21 en los requisitos de U-05, establece clips
MP4/H.264. NFR-APP-31 exige comprobar esa capacidad real antes de cargar React.
LC-APP-01 y TASK-416 ilustran la consulta con `video/mp4; codecs="avc1"`, pero ese
identificador sin perfil/nivel devuelve `""` en Firefox 157 aunque exista H.264.

La implementación precisa la consulta a `video/mp4; codecs="avc1.42E01E"`:
`42` identifica Baseline, `E0` sus indicadores de compatibilidad (Constrained
Baseline) y `1E` el nivel 3.0, según la notación AVC de RFC 6381 §3.3.
Es una **sonda de capacidad**, no una nueva exigencia de perfil, resolución o nivel
para los clips del nodo. Tampoco demuestra la decodificación de cada clip: eso se
comprueba en el reproductor. No se acepta `video/mp4` sin códec como alternativa.

Se conserva el receptor nativo de VIG-342, las respuestas permitidas (`maybe` y
`probably`) y el fallo cerrado ante respuesta vacía, excepción o método ausente.
No cambian las otras capacidades ni la política de versiones ni los flujos por rol.

## Regresión reproducible

Desde `frontend/`, con Node ≥ 22, OpenSSL, Bash y los motores de Playwright 1.64.1
(Chromium gráfico requiere pantalla o `xvfb-run -a`):

```sh
npm run test:bootstrap:browsers
```

La prueba sirve el módulo real por HTTPS local con certificado efímero en memoria,
usa `browserProbe()` y `start()` sin capacidades inyectadas e importa React solo
desde el callback admitido por la puerta. Verifica también la sección de rechazo y
las solicitudes de React. El HTML de prueba no arranca el resto de la aplicación:
los recorridos completos con la plataforma real pertenecen a VIG-299.

El cuarto proyecto usa Firefox con los decodificadores FFmpeg/GMP desactivados
mediante preferencias del motor: H.264 devuelve `""`, mientras VP8 sigue disponible.
Nunca se reemplaza `canPlayType`, el agente de usuario ni las demás capacidades.

Si faltan bibliotecas del sistema, se puede usar la imagen oficial, sin parchear el
entorno de los motores (montar solo este workspace, no credenciales):

```sh
docker run --rm --init --ipc=host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -v "$PWD:/work" -w /work \
  mcr.microsoft.com/playwright:v1.64.1-noble \
  xvfb-run -a npm run test:bootstrap:browsers
```

Mutación: sustituir **solo** `H264_TYPE` por `video/mp4; codecs="avc1"`, ejecutar
`npm run test:bootstrap:browsers -- --project firefox`, observar el rechazo nativo
y la falta de carga de React; restaurar y repetir hasta verde. La evidencia real
de ambas ejecuciones se entrega en el workpad y el PR.

# TASK-572 · Evidencia local: foto del episodio de la sombra por mTLS (A-112)

Base: `main` en `a4907de`. Rama `torre/episode-photo-ingesta`. Datos solo sintéticos (las imágenes
son JPEG construidos en las pruebas o la imagen sintética del generador del kit). Sin cambios en
`.github/workflows`, `CODEOWNERS`, `AGENTS.md` ni `.agents/`. No se tocó Linear, el nodo de
planta, la plataforma de integración del PC ni otros repositorios.

La sesión que construyó este cambio no puede usar git fuera de su worktree: las pruebas corrieron
sobre una copia del árbol en WSL (`uv sync --frozen`, Python 3.12) y los commits se crearon con la
API de GitHub. Las pruebas de integración usaron testcontainers con el adaptador `port_retry` de
`docs/evidence/VIG-394/EVIDENCE.md` (solo reintenta el mapeo de puertos de Docker Desktop).

El detalle de criterios, mutaciones y salidas está en el cuerpo del PR; aquí quedan el manifiesto
de mutaciones ([guardas.toml](guardas.toml)) y su salida real ([MUTATIONS.txt](MUTATIONS.txt)).

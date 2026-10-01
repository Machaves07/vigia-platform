# `dist/` de ejemplo de la aplicación de página única

Construcción sintética que usan `tests/examples/test_static_serving.py` y las pruebas de
`shared.api.static` (TASK-138). No viene de U-05 ni contiene datos reales.

| Archivo | Para qué |
|---|---|
| `index.html` | Índice con el marcador `vigia-app` |
| `version.json` | `{app_version, contract_tag, built_at}` |
| `assets/index-3f9a1c2b.js` (+ `.br`, `.gz`) | Recurso con las dos variantes precomprimidas. El `.br` es un flujo brotli válido con un meta-bloque sin comprimir (RFC 7932 §9.2); el `.gz`, gzip con `mtime=0` |
| `assets/index-7d2e4f10.css` (+ `.gz`) | Recurso solo con `gzip`: un cliente que pide `br` recibe `gzip` |
| `assets/logo-5b8c0d1e.svg` | Recurso sin variantes: se sirve tal cual |
| `me` | Archivo fuera de `assets/`: nunca se sirve y nunca oculta `GET /me` |
| `robots.txt` | Distinto del cerrado: la plataforma sirve siempre el suyo |
| `README.md` | Este archivo: fuera de `assets/`, tampoco se sirve |

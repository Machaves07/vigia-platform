# WebKit compatible con la CSP estricta al crear `<select>`

Fecha: 2026-10-25 · VIG-345 (seguimiento de VIG-299)

## Defecto

Con `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'`, un
guion externo que solo ejecuta `document.body.appendChild(document.createElement("select"))`
produce un evento `securitypolicyviolation` con `style-src-attr` (línea 1, columna 49) y el aviso
de consola «Refused to apply a stylesheet…». La aplicación no asigna ningún estilo: el atributo lo
pone el propio motor al construir el control. React lo dispara en el armazón
(`ownerDocument.createElement("select")`), y la guarda de VIG-299 falla, como debe.

| Compilación de WebKit | Playwright | Anfitrión | `select` | sin `select` | estilo en línea |
| --- | --- | --- | --- | --- | --- |
| 2359 (26.6) | 1.63.0 | Ubuntu 22.04, WPE sin interfaz | 0 eventos | 0 eventos | `style-src-attr` |
| 2359 (26.6) | controlador 1.64.1 | Ubuntu 22.04, WPE sin interfaz | 0 eventos | 0 eventos | `style-src-attr` |
| 2359 (26.6) | controlador 1.64.1 | imagen oficial `v1.64.1-noble` | 0 eventos | 0 eventos | `style-src-attr` |
| 2370 (27.2) | 1.64.1 | Ubuntu 22.04, WPE y GTK | `style-src-attr` | 0 eventos | `style-src-attr` |
| 2370 (27.2) | 1.64.1 | imagen oficial `v1.64.1-noble` | `style-src-attr` | 0 eventos | `style-src-attr` |
| 2380 (27.2) | 1.65.0-alpha-2026-10-25 | Ubuntu 22.04, WPE sin interfaz | `style-src-attr` | 0 eventos | `style-src-attr` |

El defecto llega con la revisión 2370 y sigue en la 2380, la última publicada el 2026-10-25.
No depende del anfitrión: la imagen oficial, con las bibliotecas del sistema instaladas por
Playwright, lo reproduce igual. Las opciones `--features` del MiniBrowser (`-HTMLEnhancedSelect`,
`-CSSAppearanceBase`) no afectan a los contextos que crea Playwright, así que no hay interruptor
del motor que sirva de alternativa.

## Decisión

Las pruebas de navegador usan WebKit **2359** (26.6) con el controlador de `@playwright/test`
1.64.1, que no cambia. No se toca la CSP, ni la guarda, ni el selector del armazón, y no se
filtra ningún tipo de violación. Solo cambia el ejecutable del proyecto `webkit`.

- `frontend/tests/browser/webkit-build.json` fija la revisión, los espejos de Playwright y el
  SHA-256 de cada descarga (Ubuntu 22.04 y 24.04, x64).
- `node scripts/install_webkit.mjs` (desde `frontend/`) descarga, verifica el SHA-256 **antes**
  de descomprimir y deja el motor en `~/.cache/vigia/webkit-2359` (o `VIGIA_WEBKIT_DIR`). Queda
  fuera de `~/.cache/ms-playwright`, porque `playwright install` borra las revisiones que su
  versión no declara. Si el resumen no coincide, termina con error y no instala nada.
- `frontend/tests/browser/webkit.ts` exporta `compatibleWebkitExecutable()`, que el proyecto
  `webkit` usa como `launchOptions.executablePath`. Si el motor no está instalado, Playwright
  falla al lanzarlo: nunca vuelve en silencio a la 2370.
- `frontend/tests/browser/csp_select.spec.ts` es la sonda mínima como regresión en los cuatro
  proyectos: `select` y la página de control no producen eventos, y un estilo en línea
  intencional sí produce `style-src-attr`.

Los proyectos WebKit de `playwright.config.ts` de VIG-299 deben usar el mismo
`compatibleWebkitExecutable()` al integrar `main`.

Revisar la fijación cuando Playwright publique una revisión de WebKit sin el defecto: basta con
cambiar `webkit-build.json` y repetir `csp_select.spec.ts`. Que Safari 27 tenga el mismo defecto
fuera de Playwright no está comprobado.

## Reproducción

```sh
cd frontend
node scripts/install_webkit.mjs
npm run test:bootstrap:browsers -- --project webkit
```

Con la imagen oficial (motor para Ubuntu 24.04 montado en solo lectura):

```sh
VIGIA_WEBKIT_PLATFORM=ubuntu24.04-x64 VIGIA_WEBKIT_DIR=/tmp/webkit-2359-noble \
  node scripts/install_webkit.mjs
docker run --rm --init --ipc=host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e VIGIA_WEBKIT_DIR=/wk -v /tmp/webkit-2359-noble:/wk:ro \
  -v "$PWD:/work" -w /work mcr.microsoft.com/playwright:v1.64.1-noble \
  xvfb-run -a npm run test:bootstrap:browsers
```

Mutación: `VIGIA_WEBKIT_DIR=~/.cache/ms-playwright/webkit-2370` apunta el mismo proyecto a la
compilación con el defecto. «crear un select…» falla con `["console", "style-src-attr"]`, y las
otras dos pruebas siguen igual.

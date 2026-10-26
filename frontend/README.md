# `frontend/` — aplicación de Vigía (U-05)

Aplicación de página única de la plataforma: React 19, TypeScript 5 estricto y Vite, servida como
salida estática desde el mismo origen que `vigia-api` (la imagen la copia en `/app/static`). Este
documento es la documentación mínima de NFR-APP-49: instalación, construcción, pruebas, política de
navegadores, presupuesto de dependencias y cómo añadir una pantalla. Los procedimientos de
operación de la aplicación están en los [runbooks u05-6.1 a 6.4](../docs/runbooks/README.md) y en
el [manual de soporte del código de correlación](../docs/runbooks/u05-soporte-correlation-id.md).

Comentarios y textos en español; identificadores, archivos y claves en inglés.

## Instalación

Requisitos:

- Node.js 22 o posterior (`engines` de `package.json`; la imagen usa la LTS 22).
- Acceso de lectura a `Machaves07/vigia-contracts`: `@vigia/contracts` se instala por `git+ssh`
  desde la etiqueta `v1.1.0` (ADR-004), con hash fijado en `package-lock.json`. En la canalización
  se usa la clave de despliegue de solo lectura.
- Para los recorridos de extremo a extremo: Docker, los navegadores de Playwright
  (`npx playwright install --with-deps chromium firefox webkit`) y una pantalla o Xvfb (Chromium
  corre gráfico). Detalle en [`tests/e2e/README.md`](tests/e2e/README.md).

```sh
npm ci
```

`npm ci` instala exactamente lo de `package-lock.json`; nunca se usa `npm install` para preparar
el entorno.

## Entorno local y `VITE_API_MODE`

El servidor de desarrollo (`npm run dev`, `http://localhost:5173`) trabaja en uno de dos modos.
`VITE_API_MODE` se lee de una variable de entorno: no hay archivos `.env` en el repositorio (el
`.gitignore` de la raíz los excluye).

| Modo                       | Orden                                  | Qué responde a la API                                                |
| -------------------------- | -------------------------------------- | -------------------------------------------------------------------- |
| `real` (por omisión)       | `npm run dev`                          | `vigia-api` local por el proxy de Vite (`VITE_API_PROXY`, `:8000`)   |
| `mock` (pantallas sin API) | `VITE_API_MODE=mock npm run dev`       | MSW en el navegador, con los manejadores generados desde `app.yaml`  |
| `real` en compose (perfil) | `make frontend-dev` (raíz)             | Igual que `real`, con el servicio `frontend` de `docker-compose.yml` |
| `mock` en compose (perfil) | `VITE_API_MODE=mock make frontend-dev` | Igual que `mock`; compose pasa `VITE_API_MODE` al contenedor         |

- **`real`** reenvía a `vigia-api` toda petición que no sea de navegación (D-3:
  `Sec-Fetch-Mode: navigate`, o `Accept` con `text/html` y sin `application/json`); la navegación
  recibe `index.html`. `VITE_API_PROXY` cambia el destino (solo un origen `http(s)://` sin ruta ni
  credenciales).
- **`mock`** no usa el proxy: `src/main.tsx` activa `src/mocks/browser.ts`, que sirve todas las
  operaciones de `app.yaml` con los manejadores de `tests/msw/handlers` (VIG-286). MSW nunca llega
  a `dist/` (`tests/unit/build/no_msw_in_dist.test.ts`).
- **`npm run dev -- --mode mock` no activa el modo `mock`**: solo cambia el modo de Vite (el
  aviso de arranque dice `mock`), y sin un `.env.mock` `VITE_API_MODE` queda vacío, es decir,
  `real`. Usa la variable de entorno.

El modo `mock` anuncia en `/me` y `/version.json` la versión incrustada de Vite y la etiqueta del
contrato fijada en `package.json`: cargar la sesión no bloquea las escrituras por una versión
aleatoria. Vitest usa una construcción sintética estable (`dev` y la misma etiqueta fijada).

### API real local y comprobación de versión

Sin estáticos, el backend anuncia `unknown` y `/version.json` devuelve 404. Eso **no** coincide con
el `dev` de Vite: el bloqueo `version_outdated` es deliberado y recargar no lo resuelve. No se
exceptúa `unknown` ni se desactiva la guarda en desarrollo. Para trabajar con API real, prepara
los metadatos de la misma construcción y reinicia la API (los lee al arrancar). Desde la raíz:

```sh
VITE_APP_VERSION=dev npm --prefix frontend run build
VIGIA_STATIC_DIR="$PWD/frontend/dist" make run
# En otra terminal, desde la raíz:
VITE_API_MODE=real VITE_APP_VERSION=dev npm --prefix frontend run dev
```

Si usas una etiqueta o un sha en vez de `dev`, pásalo igual a ambas órdenes de frontend. Mantén
la construcción estática actualizada al cambiar el contrato. Las dependencias locales de
`make run` se preparan como de costumbre (`make up`, migraciones).

## Órdenes

| Orden                    | Qué hace                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------- |
| `npm run dev`            | Servidor de desarrollo en `http://localhost:5173` (modo en la sección anterior)       |
| `npm run build`          | Construcción de producción en `dist/` (nombres con hash, sin mapas de fuente)         |
| `npm run preview`        | Sirve `dist/` en local                                                                |
| `npm run lint`           | ESLint (NFR-APP-47) y `prettier --check`                                              |
| `npm run typecheck`      | `tsc --noEmit`                                                                        |
| `npm run knip`           | Dependencias, archivos y exportaciones sin uso (`knip.json` más `knip.ignore.*.json`) |
| `npm test`               | vitest con el perfil `ci` de fast-check (200 ejemplos)                                |
| `npm run test:nightly`   | vitest con el perfil `nightly` (2 000 ejemplos)                                       |
| `npm run check`          | Cada comprobación de `scripts/checks/*.mjs`, en orden                                 |
| `npm run generate`       | Cliente, validadores y esquemas espejo desde `backend/openapi/app.yaml`               |
| `npm run generate:check` | Falla con la diferencia si algo generado no coincide con `app.yaml`                   |
| `npm run e2e`            | Playwright contra la plataforma real local, en seis proyectos (VIG-299)               |
| `npm run size`           | Presupuesto de tamaño por porción (lo completa VIG-294)                               |
| `npm run lighthouse`     | Lighthouse CI sobre la construcción servida por el mundo (`lighthouserc.json`)        |

`npm test -- <ruta>` limita vitest a unos archivos o carpetas, por ejemplo
`npm test -- tests/unit/docs`. Desde la raíz, `make frontend-test`, `make frontend-check` y
`make frontend-e2e` envuelven `npm test`, `npm run check` y los recorridos.

## Construcción

`npm run build` deja en `dist/`:

- `index.html` con el marcador `vigia-app` (la construcción falla si falta) y sin guiones ni
  estilos en línea;
- `assets/` con hash de contenido, una porción por pantalla (`scrNN-<hash>.js`) y sus `.br` y `.gz`
  precomprimidos;
- `version.json` con `app_version` (de `VITE_APP_VERSION`, por omisión `dev`), `contract_tag` y
  `built_at`.

Los mapas de fuente nunca están en `dist/`: van a `../frontend-sourcemaps/` (o a
`VIGIA_SOURCEMAPS_DIR`), se quedan en la etapa `frontend-build` y no llegan a la imagen
(NFR-APP-24); el release los adjunta para diagnóstico (VIG-337).
`src/bootstrap/minVersions.generated.ts` se regenera en cada construcción desde `browserslist`
(ver [Política de navegadores](#política-de-navegadores)).

`vigia-api` sirve `index.html` y `version.json` con `no-store`, y `assets/*` con
`Cache-Control: public, max-age=31536000, immutable`
(`backend/src/vigia_platform/shared/api/static.py`).

## Pruebas

| Carpeta              | Qué contiene                                                                       |
| -------------------- | ---------------------------------------------------------------------------------- |
| `tests/unit/`        | Reductores, mapa de errores, arranque, construcción, puertos, textos y este README |
| `tests/components/`  | Testing Library con MSW y axe, una carpeta por pantalla (`scrNN/`)                 |
| `tests/properties/`  | PR-APP-01 a 30 (fast-check)                                                        |
| `tests/views/`       | Registro de vistas que recorren PR-APP-07 y PR-APP-10                              |
| `tests/msw/`         | Manejadores generados y escenarios (`tests/msw/scenarios/`)                        |
| `tests/abuse/`       | Escenarios A-1 a A-10                                                              |
| `tests/resilience/`  | FS-APP-01 a 10                                                                     |
| `tests/quality/`     | Catálogo de textos, etiquetas y calidad transversal                                |
| `tests/regressions/` | Contraejemplos reducidos convertidos en prueba permanente (PBT-10)                 |
| `tests/e2e/`         | Playwright contra el mundo real de U-05 ([README propio](tests/e2e/README.md))     |

### Recorrido de humo

Con Docker en marcha, desde `frontend/` (detalle y variables en
[`tests/e2e/README.md`](tests/e2e/README.md)):

```sh
(cd ../backend && uv sync --frozen)
npm run build
node scripts/install_webkit.mjs
npm run e2e -- tests/e2e/smoke.spec.ts
```

El mundo levanta PostgreSQL y LocalStack propios, la API con la raíz de producción y `dist/`, y
cuentas sintéticas; al terminar los libera. La política de contenido y los tipos confiables están
activos y una violación falla la prueba.

### Semillas de fast-check

Cada corrida imprime al empezar el perfil y la semilla:

```text
[fast-check] perfil ci: 200 ejemplos, semilla 1488644706 (repítela con FC_SEED=1488644706)
```

`FC_SEED=<semilla>` repite la corrida; `FC_PROFILE=nightly` usa 2 000 ejemplos. Todo contraejemplo
reducido se guarda como prueba de regresión en `tests/regressions/` (PBT-10).

### Rendimiento (NFR-APP-02, 03, 05 a 07)

- `npm test -- tests/perf`: marcas de interacción (tendencia en `ci`, bloqueantes con
  `npm run test:nightly`), consumo de la API en reposo y pestañas sin caché compartida.
- `npm run e2e -- tests/e2e/memory.spec.ts`: montón de la pestaña con la bandeja de 200 filas
  (Chromium de escritorio).
- `npm run lighthouse`: arranca el mundo con `perf/world-server.mjs` (puerto fijo `47330`), tres
  corridas con 10 Mbit/s y CPU ×4 simulados, y falla si la mediana supera el umbral de
  `perf/baseline.json` (mediana calibrada + 20 %, sin pasar del presupuesto). Chrome corre con
  interfaz gráfica y su agente de usuario nativo: el emulado de Lighthouse no pasa la comprobación
  de navegadores. En WSL, `CHROME_PATH` debe apuntar al Chromium de Playwright (si no, se elige el
  Chrome de Windows). Recalibrar: tres corridas y
  `node perf/calibrate.mjs --write --machine "<equipo>" --load "<carga>"`.

## Canalización

Lo que fija `tech-stack-decisions.md` §5 de U-05 para `frontend/`:

| Flujo         | Trabajos de `frontend/`                                                                                                                                                                                                                                 |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ci.yml`      | `npm ci`; `generate --check`; `tsc --noEmit`; ESLint y Prettier; `knip`; textos y etiquetas; destinos de red; vitest unitarias, componentes y propiedades (perfil `ci`) con cobertura; axe; `npm audit`; `gitleaks`; construcción con informe de tamaño |
| `nightly.yml` | Propiedades con el perfil `nightly`; Playwright en Chromium, Firefox y WebKit, escritorio y tableta, contra el backend local; Lighthouse CI; medición de memoria                                                                                        |
| `release.yml` | Construcción reproducible dentro de la imagen de `vigia-api` (etapa `frontend-build`, `app_version` y `version.json`); inventario CycloneDX de npm; hash del paquete; mapas de fuente adjuntos y no servidos                                            |

Estado en `main`:

- **`ci.yml`**: el trabajo `frontend` lo añade VIG-294 (TASK-412), que fusiona el dueño. Hasta
  entonces, un PR que solo toca `frontend/` o `docs/` salta los trabajos de backend, y la imagen
  `arm64` de `ci.yml` ya construye la etapa `frontend-build`.
- **`nightly.yml`**: los trabajos de `frontend-nightly` los añade VIG-337 (TASK-456), que fusiona el
  dueño; VIG-339 los contrasta con la ejecución real.
- **`release.yml`**: tras desplegar, `backend/tools/deploy_checks.py` corre la comprobación nº 8 en
  `staging-<n>` (recorrido de Playwright con la organización sintética y cero violaciones de la
  política de contenido) y la nº 9 en `pilot` (`GET /` con `vigia-app`, `/version.json` con el
  `app_version` de la release, paquete inicial `200 immutable` y `vigia-app-index` en `OK`),
  VIG-319. Las dos se ensayan en local contra el mundo ([`tests/e2e/README.md`](tests/e2e/README.md)).

## Política de navegadores

Política de NFR-APP-31 (cierra R12 de U-02), que se cita en el acta de comisionamiento:
navegadores de actualización continua en sus **dos últimas versiones mayores**, en escritorio y
tableta. La fija `browserslist` de `package.json`, con estas consultas:

- `last 2 Chrome versions`
- `last 2 Edge versions`
- `last 2 Firefox versions`
- `last 2 Safari versions`
- `not IE 11`

Versión mínima que resuelve hoy `browserslist` con el `caniuse-lite` del `package-lock.json`. La
construcción la escribe en `src/bootstrap/minVersions.generated.ts`, y el arranque rechaza
cualquier versión anterior:

| Motor   | Versión mínima | Dónde                        |
| ------- | -------------- | ---------------------------- |
| Chrome  | 155            | Escritorio y tableta Android |
| Edge    | 154            | Escritorio                   |
| Firefox | 157            | Escritorio                   |
| Safari  | 26.6           | macOS y iPadOS               |

- **Fuera de la política**: Internet Explorer, Edge heredado (`Edge/`), Opera, Samsung Internet y
  otras marcas que comparten motor, navegadores integrados en aplicaciones (vistas web de Android,
  Electron, Facebook, Instagram…) y los navegadores de iOS que no son Safari (`CriOS`, `FxiOS`,
  `EdgiOS`). La lista está en `src/bootstrap/check.ts`.
- **Comprobación del arranque** (LC-APP-01, UI-01): antes de cargar React y del inicio de sesión,
  `src/bootstrap/check.ts` comprueba `fetch`, `Intl` con zonas horarias, `crypto.subtle` (SHA-256 de
  las subidas), `video` con H.264 y la versión del navegador. Si falta algo, muestra la sección
  estática «Navegador no compatible» de `index.html` y no hace ninguna petición.
- **Pruebas**: `tests/unit/bootstrap/` y `tests/properties/pr_app_26_capabilities.test.ts` en
  `ci`; los recorridos de Playwright en Chromium, Firefox y WebKit, escritorio y tableta.
  `tests/unit/docs/browsers_policy.test.ts` comprueba que esta sección coincide con
  `browserslist`: si una actualización de `caniuse-lite` mueve las versiones, la prueba falla y esta
  tabla se actualiza en el mismo cambio, igual que el archivo generado.

## Presupuesto de dependencias

Toda dependencia de producción necesita un motivo, y añadir una exige revisar
`tech-stack-decisions.md` §9 de U-05 y actualizar [`DEPENDENCIES.md`](DEPENDENCIES.md) en el mismo
cambio (NFR-APP-25). Versión exacta (sin `^` ni `~`) y licencia de la lista permitida (MIT, BSD-2,
BSD-3, Apache-2.0, ISC, PSF, MPL-2.0; excepciones de las adendas A-43 y A-71).

| Dependencia de producción | Motivo                                                                                 |
| ------------------------- | -------------------------------------------------------------------------------------- |
| `react`, `react-dom`      | Biblioteca de interfaz decidida en Requisitos (React 19)                               |
| `react-router`            | Enrutamiento con una porción de código por pantalla                                    |
| `@tanstack/react-query`   | Caché de consultas por pantalla con refresco condicionado a pestaña visible            |
| `openapi-fetch`           | Cliente tipado ligero sobre `fetch` (los tipos de `openapi-typescript`, en desarrollo) |
| `ajv`, `ajv-formats`      | Validación espejo con los esquemas del contrato y de la OpenAPI                        |
| `@vigia/contracts`        | Tipos, validadores, etiquetas e `isCompatible` del contrato (U-01)                     |
| `date-fns`, `date-fns-tz` | Aritmética de periodos en la zona horaria de la planta                                 |
| `recharts`                | Barras por zona y periodo, y distribución por hora (SVG, sin `eval`)                   |

Lo que **no** entra, a propósito: biblioteca de componentes, CSS en JS, biblioteca de
internacionalización (los textos son un catálogo JSON propio), cliente de telemetría o de errores
(NFR-APP-40) y reproductor de vídeo de terceros (se usa `video` nativo).

`tests/unit/build/dependencies.test.ts` compara la tabla de `DEPENDENCIES.md` con `package.json`
(nombre, tipo y versión), comprueba la licencia instalada de cada paquete y que las de producción
son exactamente las de §9. Las de desarrollo, con su motivo y su licencia, también están en
`DEPENDENCIES.md`.

## Cómo añadir una pantalla

Cada pantalla `SCR-NN` es una porción propia, con su descriptor, su puerto, sus textos, su vista
registrada y sus pruebas. SCR-10 (cola de revisión) es el ejemplo más corto que copiar.

1. **Descriptor** (`src/pages/scrNN/descriptor.ts`). Exporta `descriptor: ScreenDescriptor`
   (`src/app/descriptor.ts`): `screen_id`, `title_text` (clave `navigation.scrNN`), `route`,
   `required_permissions` (lista no vacía; basta una), `route_groups` (cada grupo con `id`, `paths`,
   `access`, `required_permissions` y `load: () => import('./<grupo>')`), `sections`,
   `expected_roles`, `backend_unit`, `home_for_roles`, `home_group` y `device_classes`.
   - Cada grupo es una carpeta `src/pages/scrNN/<grupo>/` con `index.tsx` de exportación por
     omisión.
   - Las claves de permiso son literales, existen en `backend/openapi/u05-fixtures.json` y nunca
     son `platform.*` (`scripts/checks/permissions.mjs`).
   - El descriptor se añade a `SCREENS` de `src/app/screens.ts`. Cambian con él
     `tests/unit/app/screens.test.ts` (número y orden de pantallas), `tests/unit/app/relay_files.test.ts`
     (número de grupos) y `tests/unit/build/screen_chunks.test.ts` (una porción por pantalla).
2. **Puerto** (`src/ports/scrNN<Nombre>.ts`). `defineScreenPort('SCR-NN', { … })` de
   `src/ports/base` declara cada operación con `kind` (`read` o `write`), `status` (`fixed` con su
   `operationId` de `app.yaml`, o `assumed`, que responde `unavailable` sin llamar a la red),
   y `permission`. El transporte, los tiempos (10 s lectura, 30 s escritura), los reintentos
   de lectura (2, 4 y 8 s; ninguno en escritura) y la validación de cuerpos ya están en `ports.base`.
   - **La página importa solo su puerto** (BR-APP-62): desde `src/pages/scrNN/**` solo se importan
     `src/ports/scrNN*` y `src/ports/shared*`; nunca `@/client`, `ports/base`, `errors`,
     `validation` ni el puerto de otra pantalla (`tests/unit/ports/only_own_port.test.ts`; la regla
     de ESLint impide importar `src/client` fuera de `src/ports`).
3. **Textos** (`src/texts/scrNN.json`). Claves en inglés de la forma `scrNN.<parte>`, valores en
   español; se leen con `text('scrNN.<parte>')` de `@/texts` y las listas cerradas con
   `label('<enumeración>', valor)` de `@/texts/labels`. Ningún valor crudo se muestra a una persona.
   - Una clave repetida entre archivos o un valor vacío fallan al cargar.
   - `scripts/checks/texts.mjs` rechaza el vocabulario de seguridad y productividad («seguro»,
     «despejado», «garantiza»…) y exige «objetivo propio» en las claves `*.goal`.
     `backend/tests/unit/test_u05_texts_vocabulary.py` pasa el validador `blocked_vocabulary` de
     U-04 por todos los textos (rechaza, por ejemplo, «ausencia»).
4. **Vista registrada** (`tests/views/scrNN.ts`). `export default { <nombre>: ViewRegistration }`
   (`tests/views/types.ts`) con `screen`, `view` (`RV-NN`), `data` (un arbitrario, casi siempre
   `readView('<Esquema>')`), `render`, `allowedFields` y, si la vista es de una organización,
   `organizationOf`. PR-APP-07 (ningún dato de persona ni campo fuera de `allowedFields`) y
   PR-APP-10 (nada de otra organización) recorren todas las vistas registradas.
5. **Pruebas**:
   - de componente en `tests/components/scrNN/` con MSW (`server.use(...)`, `apiError`,
     `exampleFor`) y `expect(await axe(container)).toHaveNoViolations()`;
   - de propiedades en `tests/properties/` si la pantalla añade reglas;
   - escenarios de MSW reutilizables en `tests/msw/scenarios/<escenario>.ts`;
   - el recorrido del rol que la usa en `tests/e2e/<rol>.spec.ts`.
6. **Comprueba**: `npm run lint`, `npm run typecheck`, `npm run knip`, `npm run check`,
   `npm test` y el recorrido afectado.

## Errores y soporte

Todo error que se muestra a una persona lleva el `correlation_id` de la respuesta, copiable, con la
indicación «Indique este código al reportar» (UI-19, NFR-APP-41): en la pila de avisos
(`src/components/NoticeStack/`) y en la pantalla de error global (`src/app/GlobalErrorHandler.tsx`).
La interfaz no instrumenta ni reporta nada (NFR-APP-40): sin telemetría, sin ruta de errores del
cliente y sin `console` en producción. Las alertas viven en el servidor (NFR-APP-29). Cómo se busca
el código en las trazas, en el
[manual de soporte](../docs/runbooks/u05-soporte-correlation-id.md).

## Archivos por descubrimiento

Para que las tareas no editen archivos compartidos, cada una añade archivos propios:

- `src/texts/<dominio>.json`: textos con claves en inglés; dos archivos con la misma clave fallan.
- `scripts/checks/<comprobación>.mjs`: comprobaciones de `npm run check`.
- `tests/views/<vista>.ts` y `tests/msw/scenarios/<escenario>.ts`: `export default { nombre: valor }`.
- `knip.ignore.<tarea>.json`: dependencias aún sin uso; la tarea que las usa borra su archivo.

`package.json`, `package-lock.json`, `eslint.config.js`, `tsconfig.json` y `vitest.config.ts` son
del andamiaje (VIG-277); `vite.config.ts` solo lo amplía VIG-288. Las dependencias y su licencia
están en `DEPENDENCIES.md`.

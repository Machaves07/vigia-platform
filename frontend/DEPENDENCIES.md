# Dependencias de `frontend/`

Presupuesto de dependencias de la aplicación (NFR-APP-25; `tech-stack-decisions.md` §9 de U-05).
Toda dependencia lleva versión exacta (sin `^` ni `~`), licencia de la lista permitida (MIT, BSD-2,
BSD-3, Apache-2.0, ISC, PSF, MPL-2.0 sin modificar; excepciones de la adenda A-43) y motivo.
Añadir una dependencia exige revisar §9 y actualizar esta tabla en el mismo cambio.

`tests/unit/build/dependencies.test.ts` compara esta tabla con `package.json` por igualdad
(nombre, tipo y versión), comprueba la licencia contra el `package.json` instalado de cada
paquete y que las dependencias de producción son exactamente las de §9.

Columna «Presupuesto»:

- **§9**: nombrada en `tech-stack-decisions.md` §9 (producción) o en su lista de desarrollo.
- **auxiliar**: no la nombra §9, pero la exige una de §9 (tipos de TypeScript, par obligatorio o
  proveedor de cobertura que pide el andamiaje). Solo de desarrollo; nunca llega a `dist/`.

| Dependencia                   | Tipo       | Versión                                                          | Licencia           | Presupuesto | Motivo                                                                                               |
| ----------------------------- | ---------- | ---------------------------------------------------------------- | ------------------ | ----------- | ---------------------------------------------------------------------------------------------------- |
| `@tanstack/react-query`       | producción | `5.104.5`                                                        | MIT                | §9          | Caché de consultas por pantalla con refresco condicionado a pestaña visible                          |
| `@vigia/contracts`            | producción | `git+ssh://git@github.com/Machaves07/vigia-contracts.git#v1.1.0` | Propietaria (U-01) | §9          | Tipos, validadores Ajv, etiquetas e `isCompatible` del contrato; etiqueta git con hash en el lock    |
| `ajv`                         | producción | `8.20.0`                                                         | MIT                | §9          | Validación espejo con los esquemas derivados de la OpenAPI (la misma versión que el contrato)        |
| `ajv-formats`                 | producción | `3.0.1`                                                          | MIT                | §9          | Formatos `date-time`, `uuid` y `email` de la validación espejo                                       |
| `date-fns`                    | producción | `4.4.0`                                                          | MIT                | §9          | Aritmética de periodos y tramos de 31 días                                                           |
| `date-fns-tz`                 | producción | `3.2.0`                                                          | MIT                | §9          | Zona horaria de la planta en la aritmética de periodos                                               |
| `openapi-fetch`               | producción | `0.17.0`                                                         | MIT                | §9          | Cliente tipado ligero sobre `fetch` generado desde `backend/openapi/app.yaml`                        |
| `react`                       | producción | `19.3.1`                                                         | MIT                | §9          | Biblioteca de interfaz decidida en Requisitos (React 19)                                             |
| `react-dom`                   | producción | `19.3.1`                                                         | MIT                | §9          | Representación de React en el navegador                                                              |
| `react-router`                | producción | `8.5.0`                                                          | MIT                | §9          | Enrutamiento en modo de datos con una porción de código por pantalla                                 |
| `recharts`                    | producción | `3.10.1`                                                         | MIT                | §9          | Barras por zona y periodo y distribución por hora (SVG, sin `eval`)                                  |
| `@axe-core/playwright`        | desarrollo | `4.14.0`                                                         | MPL-2.0            | §9          | Accesibilidad con axe en las pruebas de extremo a extremo                                            |
| `@cyclonedx/cyclonedx-npm`    | desarrollo | `6.0.1`                                                          | Apache-2.0         | §9          | Inventario CycloneDX de npm adjunto al release                                                       |
| `@lhci/cli`                   | desarrollo | `0.15.1`                                                         | Apache-2.0         | §9          | Lighthouse CI con limitación de red y procesador en `nightly`                                        |
| `@playwright/test`            | desarrollo | `1.64.1`                                                         | Apache-2.0         | §9          | Recorridos de extremo a extremo en Chromium, Firefox y WebKit                                        |
| `@testing-library/dom`        | desarrollo | `10.4.2`                                                         | MIT                | auxiliar    | Par obligatorio de `@testing-library/react` 16                                                       |
| `@testing-library/react`      | desarrollo | `16.3.3`                                                         | MIT                | §9          | Pruebas de componente por comportamiento                                                             |
| `@testing-library/user-event` | desarrollo | `14.6.8`                                                         | MIT                | §9          | Interacción de teclado y puntero en las pruebas de componente                                        |
| `@types/node`                 | desarrollo | `22.20.5`                                                        | MIT                | auxiliar    | Tipos de Node 22 (mínimo de `engines`) para la configuración, los guiones y las pruebas              |
| `@types/react`                | desarrollo | `19.3.0`                                                         | MIT                | auxiliar    | Tipos de `react` para TypeScript estricto                                                            |
| `@types/react-dom`            | desarrollo | `19.3.0`                                                         | MIT                | auxiliar    | Tipos de `react-dom` para TypeScript estricto                                                        |
| `@vitejs/plugin-react`        | desarrollo | `6.1.2`                                                          | MIT                | §9          | JSX y recarga en caliente de React en Vite                                                           |
| `@vitest/coverage-v8`         | desarrollo | `5.0.5`                                                          | MIT                | auxiliar    | Proveedor de cobertura v8 de `vitest` (misma versión que `vitest`)                                   |
| `axe-core`                    | desarrollo | `4.14.0`                                                         | MPL-2.0            | §9          | Motor de accesibilidad de `vitest-axe` y `@axe-core/playwright`                                      |
| `browserslist`                | desarrollo | `4.29.4`                                                         | MIT                | auxiliar    | Versión mínima del arranque desde `browserslist` de `package.json` (NFR-APP-31)                      |
| `eslint`                      | desarrollo | `9.39.5`                                                         | MIT                | §9          | Puertas de calidad (NFR-APP-47); 9 y no 10 porque `eslint-plugin-jsx-a11y` 6 solo admite hasta la 9  |
| `eslint-plugin-jsx-a11y`      | desarrollo | `6.10.2`                                                         | MIT                | §9          | Reglas de accesibilidad en JSX                                                                       |
| `eslint-plugin-react-hooks`   | desarrollo | `7.1.1`                                                          | MIT                | §9          | Reglas de los hooks de React                                                                         |
| `fast-check`                  | desarrollo | `3.23.2`                                                         | MIT                | §9          | Pruebas basadas en propiedades (fast-check 3, heredado de U-01; misma versión que el contrato)       |
| `jsdom`                       | desarrollo | `30.1.3`                                                         | MIT                | §9          | DOM de las pruebas de vitest                                                                         |
| `knip`                        | desarrollo | `6.42.1`                                                         | ISC                | §9          | Dependencias, archivos y exportaciones sin uso (bloqueante)                                          |
| `msw`                         | desarrollo | `3.0.3`                                                          | MIT                | §9          | Simulación de la API en pruebas y en `VITE_API_MODE=mock`                                            |
| `openapi-typescript`          | desarrollo | `7.13.0`                                                         | MIT                | §9          | Tipos del cliente generados desde `backend/openapi/app.yaml`                                         |
| `prettier`                    | desarrollo | `3.9.10`                                                         | MIT                | §9          | Formato del código                                                                                   |
| `typescript`                  | desarrollo | `5.9.3`                                                          | Apache-2.0         | §9          | TypeScript 5 estricto (heredado); 5 y no 7 porque `typescript-eslint` y `openapi-typescript` piden 5 |
| `typescript-eslint`           | desarrollo | `8.72.0`                                                         | MIT                | §9          | Reglas estrictas de TypeScript con información de tipos                                              |
| `vite`                        | desarrollo | `8.3.5`                                                          | MIT                | §9          | Construcción estática con hash y servidor de desarrollo                                              |
| `vitest`                      | desarrollo | `5.0.5`                                                          | MIT                | §9          | Ejecutor de pruebas (ESM nativo)                                                                     |
| `vitest-axe`                  | desarrollo | `0.1.0`                                                          | MIT                | §9          | Comparador `toHaveNoViolations` de axe en las pruebas de componente                                  |

## Recursos incluidos que no son paquetes de npm

Tipografía del sistema visual v1 (VIG-364), servida desde la propia aplicación: la CSP es `self` y
no admite Google Fonts. Los archivos están en `src/styles/fonts/` y Vite los publica con hash en
`assets/` (lo único que sirve la API). Solo el subconjunto latino, que cubre el español.

- **IBM Plex Sans** 400, 500, 600 y 700 (woff2), de `@fontsource/ibm-plex-sans` 5.3.0: SIL Open
  Font License 1.1 (`src/styles/fonts/OFL-IBM-Plex-Sans.txt`), excepción de licencia para fuentes
  pedida por el dueño el 28-10 en VIG-364.
- **IBM Plex Mono** 400 (woff2), de `@fontsource/ibm-plex-mono` 5.3.0: SIL Open Font License 1.1
  (`src/styles/fonts/OFL-IBM-Plex-Mono.txt`). Solo para códigos copiables.

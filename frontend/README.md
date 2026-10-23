# `frontend/` — aplicación de Vigía (U-05)

Aplicación de página única de la plataforma: React 19, TypeScript 5 estricto y Vite, servida como
salida estática desde el mismo origen que `vigia-api`. Este README lo completa VIG-331 (TASK-462).

## Instalación

Requiere Node.js 22 o posterior (LTS activa) y acceso de lectura a `Machaves07/vigia-contracts`:
`@vigia/contracts` se instala por `git+ssh` desde la etiqueta `v1.1.0` (ADR-004), con hash fijado en
`package-lock.json`. En la canalización se usa la clave de despliegue de solo lectura.

```sh
npm ci
```

## Órdenes

| Orden                    | Qué hace                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------- |
| `npm run dev`            | Servidor de desarrollo en `http://localhost:5173`                                     |
| `npm run build`          | Construcción de producción en `dist/` (nombres con hash, sin mapas de fuente)         |
| `npm run preview`        | Sirve `dist/` en local                                                                |
| `npm run lint`           | ESLint (NFR-APP-47) y `prettier --check`                                              |
| `npm run typecheck`      | `tsc --noEmit`                                                                        |
| `npm run knip`           | Dependencias, archivos y exportaciones sin uso (`knip.json` más `knip.ignore.*.json`) |
| `npm test`               | vitest con el perfil `ci` de fast-check (200 ejemplos)                                |
| `npm run test:nightly`   | vitest con el perfil `nightly` (2 000 ejemplos)                                       |
| `npm run check`          | Cada comprobación de `scripts/checks/*.mjs`, en orden                                 |
| `npm run generate`       | Cliente y esquemas desde `backend/openapi/app.yaml` (lo completa VIG-280)             |
| `npm run generate:check` | Diferencia vacía de lo generado (lo completa VIG-280)                                 |
| `npm run e2e`            | Playwright contra la plataforma local (lo completa VIG-299)                           |
| `npm run size`           | Presupuesto de tamaño por porción (lo completa VIG-294)                               |

### Modo de la API en desarrollo

`VITE_API_MODE=real` (por omisión) reenvía a `vigia-api` (`http://localhost:8000`) toda petición
que no sea de navegación (D-3: `Sec-Fetch-Mode: navigate`, o `Accept` con `text/html` y sin
`application/json`); la navegación recibe `index.html`. `VITE_API_MODE=mock` no usa el proxy: las
respuestas las da MSW en el navegador (VIG-286).

### Semillas de fast-check

Cada corrida imprime al empezar el perfil y la semilla:

```text
[fast-check] perfil ci: 200 ejemplos, semilla 1488644706 (repítela con FC_SEED=1488644706)
```

`FC_SEED=<semilla>` repite la corrida; `FC_PROFILE=nightly` usa 2 000 ejemplos. Todo contraejemplo
reducido se guarda como prueba de regresión en `tests/regressions/` (PBT-10).

## Archivos por descubrimiento

Para que las tareas no editen archivos compartidos, cada una añade archivos propios:

- `src/texts/<dominio>.json`: textos con claves en inglés; dos archivos con la misma clave fallan.
- `scripts/checks/<comprobación>.mjs`: comprobaciones de `npm run check`.
- `tests/views/<vista>.ts` y `tests/msw/scenarios/<escenario>.ts`: `export default { nombre: valor }`.
- `knip.ignore.<tarea>.json`: dependencias aún sin uso; la tarea que las usa borra su archivo.

`package.json`, `package-lock.json`, `eslint.config.js`, `tsconfig.json` y `vitest.config.ts` son
del andamiaje (VIG-277); `vite.config.ts` solo lo amplía VIG-288. Las dependencias y su licencia
están en `DEPENDENCIES.md`.

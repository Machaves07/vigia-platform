/**
 * Piezas puras de `vite.config.ts` (LC-APP-29), separadas para probarlas sin arrancar Vite.
 *
 * - `screenChunkName`: una porción de código por pantalla `src/pages/scrNN` (NFR-APP-01, 02).
 * - `isNavigationRequest`: la regla D-3 de `infrastructure-design.md` §3. Solo una petición de
 *   navegación recibe `index.html`; todo lo demás es de la API y el servidor de desarrollo lo
 *   reenvía a `vigia-api`, porque varias rutas de la API sin prefijo (`/concessions`, `/zones/*`,
 *   `/fleet/*`, `/notifications/*`, `/loop/*`, `/accreditation/*`) coinciden con rutas de pantalla.
 * - `isViteOwnedPath`: lo que sirve el propio Vite en desarrollo (módulos, cliente HMR, recursos).
 * - `parseApiMode`: `VITE_API_MODE` es `mock` (MSW en el navegador) o `real` (proxy hacia la API).
 */

/** Destino del proxy de desarrollo: `vigia-api` del `docker-compose.yml` de U-02. */
export const DEV_API_TARGET = 'http://localhost:8000';

export type ApiMode = 'mock' | 'real';

const SCREEN_MODULE = /[\\/]src[\\/]pages[\\/](scr\d{2})[\\/]/;

/** Nombre de la porción de la pantalla a la que pertenece el módulo, o `null` si no es de una. */
export function screenChunkName(moduleId: string): string | null {
  const match = SCREEN_MODULE.exec(moduleId);
  return match?.[1] ?? null;
}

type HeaderValue = string | readonly string[] | undefined;

/** Mismos límites que `is_navigation` de `backend/src/vigia_platform/shared/api/static.py`. */
const MAX_HEADER_ITEMS = 64;
const QVALUE = /^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/;

function headerText(value: HeaderValue): string {
  if (value === undefined) return '';
  return typeof value === 'string' ? value : value.join(',');
}

/** `valor → q` de `Accept`; un `q` mal formado descarta la entrada y `q=0` no es aceptable. */
function qualities(header: string): Map<string, number> {
  const result = new Map<string, number>();
  for (const item of header.split(',').slice(0, MAX_HEADER_ITEMS)) {
    const [name = '', ...params] = item.split(';');
    const token = name.trim().toLowerCase();
    if (token === '') continue;
    let quality: number | null = 1;
    for (const param of params) {
      const [key = '', ...rest] = param.split('=');
      if (key.trim().toLowerCase() === 'q') {
        const value = rest.join('=').trim();
        quality = QVALUE.test(value) ? Number(value) : null;
      }
    }
    if (quality !== null) result.set(token, Math.max(quality, result.get(token) ?? 0));
  }
  return result;
}

/**
 * `true` si la petición es de navegación (D-3), con la misma regla que la API: `Sec-Fetch-Mode:
 * navigate`, o `Accept` que admite `text/html` y no admite `application/json` (`*` no cuenta).
 */
export function isNavigationRequest(headers: Readonly<Record<string, HeaderValue>>): boolean {
  if (headerText(headers['sec-fetch-mode']).trim().toLowerCase() === 'navigate') return true;
  const accepted = qualities(headerText(headers.accept));
  return (accepted.get('text/html') ?? 0) > 0 && (accepted.get('application/json') ?? 0) <= 0;
}

const VITE_OWNED = [/^\/@/, /^\/src\//, /^\/node_modules\//, /^\/__vite/, /^\/favicon\.ico$/];

/** `true` si la ruta la sirve Vite en desarrollo (nunca se reenvía a la API). */
export function isViteOwnedPath(url: string): boolean {
  const path = url.split('?', 1)[0] ?? '';
  return VITE_OWNED.some((pattern) => pattern.test(path));
}

/** Lee `VITE_API_MODE`; sin valor es `real`. Cualquier otro valor detiene la construcción. */
export function parseApiMode(value: string | undefined): ApiMode {
  if (value === undefined || value === '') return 'real';
  if (value === 'mock' || value === 'real') return value;
  throw new Error(`VITE_API_MODE debe ser «mock» o «real», no «${value}».`);
}

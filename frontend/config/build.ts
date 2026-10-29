/**
 * Piezas puras de `vite.config.ts` (LC-APP-29), separadas para probarlas sin arrancar Vite.
 *
 * - `screenChunkName`: una porción de código por pantalla `src/pages/scrNN` (NFR-APP-01, 02);
 *   `vendorChunkName`: una sola porción con las dependencias del arranque (VIG-355).
 * - `isNavigationRequest`: la regla D-3 de `infrastructure-design.md` §3. Solo una petición de
 *   navegación recibe `index.html`; todo lo demás es de la API y el servidor de desarrollo lo
 *   reenvía a `vigia-api`, porque varias rutas de la API sin prefijo (`/concessions`, `/zones/*`,
 *   `/fleet/*`, `/notifications/*`, `/loop/*`, `/accreditation/*`) coinciden con rutas de pantalla.
 * - `isViteOwnedPath`: lo que sirve el propio Vite en desarrollo (módulos, cliente HMR, recursos).
 * - `parseApiMode`: `VITE_API_MODE` es `mock` (MSW en el navegador) o `real` (proxy hacia la API);
 *   `parseApiProxy`: `VITE_API_PROXY`, destino del proxy.
 * - Release (VIG-288): `app_version`, `contract_tag` y `built_at` de `version.json` con los
 *   patrones que valida `StaticSite`, el marcador `vigia-app` y qué archivos se precomprimen o son
 *   mapas de fuente. El complemento que los aplica está en `config/release.ts`.
 */

/**
 * Destino por omisión del proxy de desarrollo: `vigia-api` de `make run`. `VITE_API_PROXY` lo
 * cambia (el servicio `frontend` de `docker-compose.yml` usa `http://host.docker.internal:8000`).
 */
export const DEV_API_TARGET = 'http://localhost:8000';

export type ApiMode = 'mock' | 'real';

const SCREEN_MODULE = /[\\/]src[\\/]pages[\\/](scr\d{2})[\\/](?!descriptor\.ts$)/;

/**
 * Módulos de pantalla que importa el armazón (VIG-364): la puerta de sesión y el formulario de
 * acceso de SCR-01 (con su marco, `AuthFrame`, VIG-366) y la campana de SCR-15. Van con el
 * armazón, no en la porción de su pantalla: dentro de ella arrastrarían al paquete inicial la
 * pantalla entera (la administración de SCR-01).
 */
const SHELL_SCREEN_MODULE =
  /[\\/]src[\\/]pages[\\/](?:scr01[\\/](?:SessionGate|PrivacyNoticeGate|privacyNotice|privacyNotices|session|scr01\.module|login[\\/](?:LoginForm|AuthFrame))|scr15[\\/](?:Bell|Bell\.module|shared))\.(?:tsx?|json|css)$/;

/**
 * Nombre de la porción de la pantalla a la que pertenece el módulo, o `null` si no es de una.
 * `src/pages/scrNN/descriptor.ts` no es de la porción: la tabla de pantallas lo importa en el
 * paquete inicial y, dentro de la porción, arrastraría todas las pantallas al arranque (VIG-290).
 * Tampoco lo son los módulos de pantalla del armazón (`SHELL_SCREEN_MODULE`).
 */
export function screenChunkName(moduleId: string): string | null {
  if (SHELL_SCREEN_MODULE.test(moduleId)) return null;
  const match = SCREEN_MODULE.exec(moduleId);
  return match?.[1] ?? null;
}

const SCREEN_TEXTS = /[\\/]src[\\/]texts[\\/](scr\d{2}(?:_[a-z0-9_]+)?)\.json$/;

/**
 * Porción de los textos diferidos de una pantalla (VIG-364): `texts-scr08_detail`, nunca
 * `scr08`, que es el nombre de la porción de código de la pantalla. `scr01.json` y `scr15.json`
 * van en el paquete inicial (los pinta el armazón) y no tienen porción propia.
 */
export function screenTextsChunkName(moduleId: string): string | null {
  const name = SCREEN_TEXTS.exec(moduleId)?.[1];
  if (name === undefined || name === 'scr01' || name === 'scr15') return null;
  return `texts-${name}`;
}

/** Nombre de la porción de las dependencias del arranque (`vendorChunkName`). */
const VENDOR_CHUNK = 'vendor';

const VENDOR_MODULE =
  /[\\/]node_modules[\\/](?:react|react-dom|scheduler|react-router|@tanstack[\\/](?:react-query|query-core))[\\/]/;

/**
 * `vendor` si el módulo es de React, React Router o TanStack Query, o `null`. Todas van en el
 * paquete inicial; juntas en una porción, el reparto automático de Rolldown no las parte en
 * varios archivos con sus importaciones cruzadas (VIG-355: tras VIG-334 el paquete inicial superó
 * los 250 KB de NFR-APP-01).
 */
export function vendorChunkName(moduleId: string): string | null {
  return VENDOR_MODULE.test(moduleId) ? VENDOR_CHUNK : null;
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

/** Lee `VITE_API_PROXY`; sin valor, `DEV_API_TARGET`. Solo `http(s)://` sin ruta ni credenciales. */
export function parseApiProxy(value: string | undefined): string {
  if (value === undefined || value === '') return DEV_API_TARGET;
  let url: URL | null = null;
  try {
    url = new URL(value);
  } catch {
    url = null;
  }
  if (
    url === null ||
    !['http:', 'https:'].includes(url.protocol) ||
    url.username !== '' ||
    url.password !== '' ||
    url.pathname !== '/' ||
    url.search !== '' ||
    url.hash !== ''
  ) {
    throw new Error(`VITE_API_PROXY debe ser un origen http(s):// sin ruta, no «${value}».`);
  }
  return url.origin;
}

// --- Release: versión, `version.json`, precompresión y marcador (VIG-288, LC-APP-29) ----------

/**
 * Cadena que la comprobación de Route 53 `vigia-app-index` busca en `/` (R-APP-7): la
 * construcción falla si `index.html` no la contiene.
 */
export const APP_MARKER = 'vigia-app';

/** `app_version` cuando la construcción no recibe `VITE_APP_VERSION` (desarrollo local). */
const DEFAULT_APP_VERSION = 'dev';

/**
 * Mismos patrones que `_APP_VERSION`, `_CONTRACT_TAG` y `_BUILT_AT` de
 * `backend/src/vigia_platform/shared/api/static.py`: un `version.json` que no los cumple hace que
 * `vigia-api` no arranque (`StaticSiteInvalid`), así que la construcción falla antes.
 */
const APP_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const CONTRACT_TAG_PATTERN = APP_VERSION_PATTERN;
const BUILT_AT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;

/** Lee `VITE_APP_VERSION` (etiqueta de release o sha); sin valor, `dev`. */
export function resolveAppVersion(value: string | undefined): string {
  if (value === undefined || value === '') return DEFAULT_APP_VERSION;
  if (!APP_VERSION_PATTERN.test(value)) {
    throw new Error(`VITE_APP_VERSION no tiene la forma de una etiqueta o un sha: «${value}».`);
  }
  return value;
}

/**
 * Etiqueta de `@vigia/contracts` fijada en `package.json` (`git+ssh://…#v1.1.0` → `v1.1.0`,
 * ADR-004). Sin etiqueta o con otra forma, la construcción falla.
 */
export function contractTagFrom(spec: string | undefined): string {
  const parts = spec?.split('#') ?? [];
  const tag = parts.length === 2 ? parts[1] : undefined;
  if (tag === undefined || !CONTRACT_TAG_PATTERN.test(tag)) {
    throw new Error(`@vigia/contracts debe fijarse por etiqueta (…#vX.Y.Z), no «${String(spec)}».`);
  }
  return tag;
}

/**
 * Hora de la construcción en ISO 8601 UTC con milisegundos. Con `SOURCE_DATE_EPOCH` (segundos),
 * la de esa marca, para construcciones reproducibles.
 */
export function builtAt(sourceDateEpoch: string | undefined, clock: { now(): number }): string {
  if (sourceDateEpoch === undefined || sourceDateEpoch === '')
    return new Date(clock.now()).toISOString();
  if (!/^\d{1,11}$/.test(sourceDateEpoch)) {
    throw new Error(`SOURCE_DATE_EPOCH debe ser un entero de segundos, no «${sourceDateEpoch}».`);
  }
  return new Date(Number(sourceDateEpoch) * 1000).toISOString();
}

export interface AppRelease {
  readonly app_version: string;
  readonly contract_tag: string;
  readonly built_at: string;
}

/** Contenido de `version.json` (`{app_version, contract_tag, built_at}`), comprobado. */
export function versionDocument(release: AppRelease): string {
  const checks: [keyof AppRelease, RegExp][] = [
    ['app_version', APP_VERSION_PATTERN],
    ['contract_tag', CONTRACT_TAG_PATTERN],
    ['built_at', BUILT_AT_PATTERN],
  ];
  for (const [key, pattern] of checks) {
    if (!pattern.test(release[key])) throw new Error(`version.json: ${key} no es válido.`);
  }
  const { app_version, contract_tag, built_at } = release;
  return `${JSON.stringify({ app_version, contract_tag, built_at })}\n`;
}

/** Falla si `index.html` no contiene el marcador `vigia-app` (R-APP-7). */
export function assertAppMarker(html: string): void {
  if (!html.includes(APP_MARKER)) {
    throw new Error(
      `index.html no contiene el marcador «${APP_MARKER}»: la comprobación vigia-app-index ` +
        'lo busca en / (R-APP-7).',
    );
  }
}

/** `true` si el archivo es un mapa de fuente (nunca va a `dist/`; NFR-APP-24). */
export function isSourceMap(fileName: string): boolean {
  return fileName.toLowerCase().endsWith('.map');
}

/** `true` si el archivo de salida es un recurso de `assets/` que lleva `.br` y `.gz` al lado. */
export function isPrecompressed(fileName: string): boolean {
  return (
    fileName.startsWith('assets/') &&
    !isSourceMap(fileName) &&
    !fileName.endsWith('.br') &&
    !fileName.endsWith('.gz')
  );
}

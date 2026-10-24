/**
 * Versión mínima por motor, derivada de `browserslist` (NFR-APP-31, LC-APP-01).
 *
 * La política de navegadores de U-05 es la lista `browserslist` de `package.json`: navegadores de
 * actualización continua en sus dos últimas versiones de Chrome, Edge, Firefox y Safari, sin
 * Internet Explorer ni navegadores integrados en aplicaciones. De esa lista sale la versión mínima
 * que `app.bootstrap` compara con la del navegador en marcha; el resultado se escribe en
 * `src/bootstrap/minVersions.generated.ts`, que versiona el repositorio y regenera la construcción
 * (`config/bootstrap.ts`).
 *
 * La comparación **por versión** cubre `Sec-Fetch-Site`, del que depende la barrera
 * anti-falsificación de U-02 (NFR-APP-31): todo navegador de la política lo envía. Un navegador que
 * no se reconozca o que no esté en la política se trata como no compatible (fallo cerrado,
 * PAT-APP-RES-07).
 */
import { readFileSync } from 'node:fs';

import browserslist from 'browserslist';

/** Motores de la política; son las claves del archivo generado. */
export const MIN_ENGINES = ['chrome', 'edge', 'firefox', 'safari'] as const;

type MinEngine = (typeof MIN_ENGINES)[number];

/** Mayor y menor de una versión de navegador (`155`, `26.6`). */
interface MinVersion {
  readonly major: number;
  readonly minor: number;
}

export type MinVersions = Readonly<Record<MinEngine, MinVersion>>;

/** Ruta del archivo generado, relativa a `frontend/`. */
export const MIN_VERSIONS_PATH = 'src/bootstrap/minVersions.generated.ts';

/** Una entrada de `browserslist`: familia, espacio y versión (`chrome 155`, `safari 26.6`). */
const ENTRY = /^([a-z_]+) ([0-9]+)(?:\.([0-9]+))?$/;

export interface BrowserEntry {
  readonly engine: MinEngine;
  readonly version: MinVersion;
}

/**
 * `'chrome 155'` → `chrome`, `155.0`. Una familia fuera de la política detiene la construcción en
 * vez de ignorarse: si `package.json` cambia la lista, la política se amplía a propósito.
 */
export function parseBrowserEntry(entry: string): BrowserEntry {
  const match = ENTRY.exec(entry.trim());
  const engine = match?.[1] ?? '';
  if (match === null || !isEngine(engine)) {
    throw new Error(
      `browserslist: no entiendo la entrada «${entry}»; se esperan familias de ` +
        `${MIN_ENGINES.join(', ')} con forma «familia N» o «familia N.M».`,
    );
  }
  const major = Number(match[2]);
  const minor = match[3] === undefined ? 0 : Number(match[3]);
  return { engine, version: { major, minor } };
}

function isEngine(value: string): value is MinEngine {
  return (MIN_ENGINES as readonly string[]).includes(value);
}

/** La menor versión de cada motor entre las que cubre la política resuelta. */
export function minVersionsFrom(resolved: readonly string[]): MinVersions {
  const found = new Map<MinEngine, MinVersion>();
  for (const entry of resolved) {
    const { engine, version } = parseBrowserEntry(entry);
    const previous = found.get(engine);
    if (previous === undefined || isOlder(version, previous)) found.set(engine, version);
  }
  const missing = MIN_ENGINES.filter((engine) => !found.has(engine));
  if (missing.length > 0) {
    throw new Error(
      `browserslist: la política de package.json no cubre ${missing.join(', ')}; ` +
        'la comprobación del arranque no puede quedarse sin versión mínima (NFR-APP-31).',
    );
  }
  const versions = {} as Record<MinEngine, MinVersion>;
  for (const engine of MIN_ENGINES) {
    const version = found.get(engine);
    if (version !== undefined) versions[engine] = Object.freeze(version);
  }
  return Object.freeze(versions);
}

function isOlder(candidate: MinVersion, other: MinVersion): boolean {
  return (
    candidate.major < other.major ||
    (candidate.major === other.major && candidate.minor < other.minor)
  );
}

/** Las consultas de la política tal como están en `package.json` (`browserslist`). */
export function browserPolicyQueries(packageJson: string): readonly string[] {
  const manifest = JSON.parse(packageJson) as { readonly browserslist?: unknown };
  const queries = manifest.browserslist;
  if (!Array.isArray(queries) || queries.length === 0) {
    throw new Error('package.json: falta la lista «browserslist» de la política de navegadores.');
  }
  if (!queries.every((query): query is string => typeof query === 'string')) {
    throw new Error('package.json: «browserslist» debe ser una lista de consultas de texto.');
  }
  return queries;
}

/** Las versiones concretas que cubren `queries`, con `path` como directorio de `package.json`. */
export function resolveBrowserPolicy(queries: readonly string[], path: string): readonly string[] {
  const resolved = browserslist([...queries], { path });
  if (resolved.length === 0) {
    throw new Error(`browserslist: la política «${queries.join(', ')}» no cubre ningún navegador.`);
  }
  return resolved;
}

/** El texto del archivo generado, sin formatear (lo formatea `formatMinVersions`). */
export function renderMinVersions(versions: MinVersions, queries: readonly string[]): string {
  return [
    '/**',
    ' * Versión mínima por motor de la política de navegadores (NFR-APP-31, LC-APP-01).',
    ' *',
    ' * GENERADO: no se edita a mano. Lo deriva `browserslist` de `package.json` con',
    ' * `config/minVersions.ts` en cada construcción (`npm run build`);',
    ' * `tests/unit/bootstrap/min_versions.test.ts` falla si el archivo versionado no coincide.',
    ' */',
    'export const BROWSER_POLICY_QUERIES = [',
    ...queries.map((query) => `  ${quote(query)},`),
    '] as const;',
    '',
    'export const MIN_BROWSER_VERSIONS = {',
    ...MIN_ENGINES.map((engine) => {
      const { major, minor } = versions[engine];
      return `  ${engine}: [${String(major)}, ${String(minor)}],`;
    }),
    '} as const;',
    '',
  ].join('\n');
}

/** Texto entre comillas simples, como lo deja Prettier. */
function quote(text: string): string {
  return text.includes("'") ? JSON.stringify(text) : `'${text}'`;
}

/**
 * El documento final, formateado con la configuración de Prettier del repositorio: la construcción
 * escribe el archivo, así que no puede dejar `npm run lint` en rojo. Prettier se carga aquí y no en
 * la cabecera del módulo: `vite.config.ts` lo importa, y resolver la configuración no debe pagar su
 * coste (lo pagan la construcción y la prueba del archivo generado).
 */
export async function formatMinVersions(
  source: string,
  prettierConfigPath: string,
): Promise<string> {
  const { format } = await import('prettier');
  const config = JSON.parse(readFileSync(prettierConfigPath, 'utf8')) as Record<string, unknown>;
  return format(source, { ...config, parser: 'typescript' });
}

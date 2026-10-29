/**
 * Catálogo de textos por descubrimiento (LC-APP-21, NFR-APP-52).
 *
 * Cada dominio añade su propio `src/texts/<dominio>.json` (objeto plano clave → texto, claves en
 * inglés) y este módulo los reúne con `import.meta.glob`: ninguna tarea edita un archivo
 * compartido. Dos archivos con la misma clave detienen la carga, nombrando ambos.
 * Las etiquetas de las listas cerradas no van aquí: salen de `@/contracts` (`labels`).
 *
 * Carga por ruta (VIG-364): el paquete inicial solo trae los textos del armazón (los dominios que
 * no son de una pantalla) y `scr01.json` y `scr15.json`, que el armazón pinta en cualquier ruta
 * (inicio de sesión, puerta de sesión y campana). Los demás de pantalla (`scrNN*.json`) llegan con su
 * ruta: el enrutador espera `loadScreenTexts` antes de pintarla, así que `text()` sigue siendo
 * síncrono para la pantalla.
 */

export type TextCatalog = Readonly<Record<string, string>>;

const KEY = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/;

export class TextCatalogError extends Error {
  override readonly name = 'TextCatalogError';
}

/** Comprueba un módulo `ruta → contenido` y añade sus claves a `catalog`, sin repetir ninguna. */
function addModule(
  catalog: Map<string, string>,
  origin: Map<string, string>,
  path: string,
  content: unknown,
): void {
  if (typeof content !== 'object' || content === null || Array.isArray(content)) {
    throw new TextCatalogError(`${path}: el archivo de textos debe ser un objeto plano.`);
  }
  const entries = Object.entries(content);
  for (const [key, value] of entries) {
    if (!KEY.test(key)) {
      throw new TextCatalogError(`${path}: clave «${key}» con forma no permitida.`);
    }
    if (typeof value !== 'string' || value.trim() === '') {
      throw new TextCatalogError(`${path}: la clave «${key}» debe tener un texto no vacío.`);
    }
    const previous = origin.get(key);
    if (previous !== undefined) {
      throw new TextCatalogError(`Clave «${key}» repetida en ${previous} y ${path}.`);
    }
  }
  for (const [key, value] of entries) {
    origin.set(key, path);
    catalog.set(key, value as string);
  }
}

/** Reúne los módulos `ruta → contenido`; falla ante claves repetidas o valores que no son texto. */
export function mergeTextModules(modules: Readonly<Record<string, unknown>>): TextCatalog {
  const catalog = new Map<string, string>();
  const origin = new Map<string, string>();
  for (const path of Object.keys(modules).sort()) addModule(catalog, origin, path, modules[path]);
  return Object.freeze(Object.fromEntries(catalog));
}

const live = new Map<string, string>();
const liveOrigin = new Map<string, string>();
const loaded = new Set<string>();

const store: Record<string, string> = Object.create(null) as Record<string, string>;

/** Solo este módulo escribe en el catálogo: desde fuera, toda escritura falla. */
const readOnly: ProxyHandler<Record<string, string>> = {
  set: () => false,
  defineProperty: () => false,
  deleteProperty: () => false,
  setPrototypeOf: () => false,
};

/** Catálogo cargado (de solo lectura): los textos del armazón y los de las pantallas ya abiertas. */
export const texts: TextCatalog = new Proxy(store, readOnly);

function register(modules: Readonly<Record<string, unknown>>): void {
  const pending = Object.keys(modules)
    .filter((path) => !loaded.has(path))
    .sort();
  for (const path of pending) {
    addModule(live, liveOrigin, path, modules[path]);
    loaded.add(path);
  }
  for (const [key, value] of live) store[key] = value;
}

// Los de pantalla son `scrNN*.json` (dos cifras): `screen_state.json` también empieza por «scr» y
// es del armazón.
register(
  import.meta.glob<unknown>(['./*.json', '!./scr[0-9][0-9]*.json'], {
    eager: true,
    import: 'default',
  }),
);
register(
  import.meta.glob<unknown>(['./scr01.json', './scr15.json'], { eager: true, import: 'default' }),
);

const screenModules = import.meta.glob<unknown>(
  ['./scr[0-9][0-9]*.json', '!./scr01.json', '!./scr15.json'],
  { import: 'default' },
);

/** Todos los archivos de textos que llegan con su ruta (los demás van en el paquete inicial). */
export function deferredTextFiles(): string[] {
  return Object.keys(screenModules).sort();
}

/** Prefijo de archivo de una pantalla: `SCR-08` → `scr08`. */
function filePrefix(screenId: string): string {
  return screenId.toLowerCase().replace('-', '');
}

/** Archivos diferidos de las pantallas `screenIds` (`./scr08.json`, `./scr08_detail.json`…). */
export function screenTextFiles(screenIds: readonly string[]): string[] {
  const prefixes = screenIds.map(filePrefix);
  return Object.keys(screenModules)
    .filter((path) => {
      const name = path.slice(2, -'.json'.length);
      return prefixes.some((prefix) => name === prefix || name.startsWith(`${prefix}_`));
    })
    .sort();
}

/**
 * Carga los textos de las pantallas `screenIds` (la de la ruta y las que cita); idempotente.
 * Una porción que no llega rechaza la promesa, como la porción de código de la pantalla.
 */
export async function loadScreenTexts(screenIds: readonly string[]): Promise<void> {
  const files = screenTextFiles(screenIds).filter((path) => !loaded.has(path));
  if (files.length === 0) return;
  const contents = await Promise.all(
    files.map(async (path) => {
      const load = screenModules[path];
      if (load === undefined) throw new TextCatalogError(`${path}: archivo de textos desconocido.`);
      return [path, await load()] as const;
    }),
  );
  register(Object.fromEntries(contents));
}

/** Carga todos los textos diferidos (las pruebas, que pintan pantallas sin el enrutador). */
export async function loadAllTexts(): Promise<void> {
  const contents = await Promise.all(
    Object.entries(screenModules).map(async ([path, load]) => [path, await load()] as const),
  );
  register(Object.fromEntries(contents));
}

/** `true` si `key` está en el catálogo cargado. */
export function hasText(key: string): boolean {
  return live.has(key);
}

/** Texto de `key`; una clave que no existe es un error de programación, nunca un texto vacío. */
export function text(key: string): string {
  const value = live.get(key);
  if (value === undefined) throw new TextCatalogError(`Clave de texto desconocida: «${key}».`);
  return value;
}

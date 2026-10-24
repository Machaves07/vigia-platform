/**
 * Catálogo de textos por descubrimiento (LC-APP-21, NFR-APP-52).
 *
 * Cada dominio añade su propio `src/texts/<dominio>.json` (objeto plano clave → texto, claves en
 * inglés) y este módulo los reúne en la construcción con `import.meta.glob`: ninguna tarea edita
 * un archivo compartido. Dos archivos con la misma clave detienen la carga, nombrando ambos.
 * Las etiquetas de las listas cerradas no van aquí: salen de `@/contracts` (`labels`).
 */

export type TextCatalog = Readonly<Record<string, string>>;

const KEY = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/;

export class TextCatalogError extends Error {
  override readonly name = 'TextCatalogError';
}

/** Reúne los módulos `ruta → contenido`; falla ante claves repetidas o valores que no son texto. */
export function mergeTextModules(modules: Readonly<Record<string, unknown>>): TextCatalog {
  const catalog = new Map<string, string>();
  const origin = new Map<string, string>();
  for (const path of Object.keys(modules).sort()) {
    const content = modules[path];
    if (typeof content !== 'object' || content === null || Array.isArray(content)) {
      throw new TextCatalogError(`${path}: el archivo de textos debe ser un objeto plano.`);
    }
    for (const [key, value] of Object.entries(content)) {
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
      origin.set(key, path);
      catalog.set(key, value);
    }
  }
  return Object.freeze(Object.fromEntries(catalog));
}

export const texts: TextCatalog = mergeTextModules(
  import.meta.glob<unknown>('./*.json', { eager: true, import: 'default' }),
);

/** Texto de `key`; una clave que no existe es un error de programación, nunca un texto vacío. */
export function text(key: string): string {
  const value = texts[key];
  if (value === undefined) throw new TextCatalogError(`Clave de texto desconocida: «${key}».`);
  return value;
}

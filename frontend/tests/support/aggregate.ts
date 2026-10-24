/**
 * Reúne módulos descubiertos con `import.meta.glob` (agregadores de `tests/views/` y
 * `tests/msw/scenarios/`). Cada módulo exporta por omisión un objeto `nombre → valor`; dos
 * módulos con el mismo nombre detienen la carga nombrando ambos. Sin módulos, el resultado es
 * un objeto vacío.
 */
export function aggregateModules<T>(
  kind: string,
  modules: Readonly<Record<string, unknown>>,
): Readonly<Record<string, T>> {
  const result = new Map<string, T>();
  const origin = new Map<string, string>();
  for (const path of Object.keys(modules).sort()) {
    const content = modules[path];
    if (typeof content !== 'object' || content === null || Array.isArray(content)) {
      throw new Error(`${kind}: ${path} debe exportar por omisión un objeto nombre → valor.`);
    }
    for (const [name, value] of Object.entries(content)) {
      const previous = origin.get(name);
      if (previous !== undefined) {
        throw new Error(`${kind}: «${name}» repetido en ${previous} y ${path}.`);
      }
      origin.set(name, path);
      result.set(name, value as T);
    }
  }
  return Object.freeze(Object.fromEntries(result));
}

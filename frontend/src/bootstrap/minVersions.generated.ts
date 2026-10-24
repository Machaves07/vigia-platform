/**
 * Versión mínima por motor de la política de navegadores (NFR-APP-31, LC-APP-01).
 *
 * GENERADO: no se edita a mano. Lo deriva `browserslist` de `package.json` con
 * `config/minVersions.ts` en cada construcción (`npm run build`);
 * `tests/unit/bootstrap/min_versions.test.ts` falla si el archivo versionado no coincide.
 */
export const BROWSER_POLICY_QUERIES = [
  'last 2 Chrome versions',
  'last 2 Edge versions',
  'last 2 Firefox versions',
  'last 2 Safari versions',
  'not IE 11',
] as const;

export const MIN_BROWSER_VERSIONS = {
  chrome: [155, 0],
  edge: [154, 0],
  firefox: [157, 0],
  safari: [26, 6],
} as const;

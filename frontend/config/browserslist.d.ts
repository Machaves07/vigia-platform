/**
 * Tipos de `browserslist` (MIT), que no publica declaraciones propias.
 *
 * Solo se usa `browserslist(queries, {path})`: resolver la política de navegadores de
 * `package.json` a versiones concretas. Se declara aquí en vez de añadir `@types/browserslist`
 * para no sumar una dependencia que no llega al árbol de producción.
 */
declare module 'browserslist' {
  interface Options {
    /** Directorio desde el que se lee `package.json` (y `.browserslistrc`, si lo hubiera). */
    readonly path?: string;
    /** Entorno de `browserslist`; por omisión, `production`. */
    readonly env?: string;
  }

  /** Versiones concretas (`'chrome 155'`) que cubren las consultas, de la más nueva a la más vieja. */
  function browserslist(queries: readonly string[], options?: Options): string[];

  export default browserslist;
}

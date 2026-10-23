/** Tipos mínimos de `eslint-plugin-jsx-a11y`, que no publica los suyos (solo `eslint.config.js`). */
declare module 'eslint-plugin-jsx-a11y' {
  import type { Linter } from 'eslint';

  const plugin: {
    readonly flatConfigs: {
      readonly recommended: Linter.Config;
      readonly strict: Linter.Config;
    };
  };
  export default plugin;
}

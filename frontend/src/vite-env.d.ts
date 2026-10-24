/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `mock` (MSW en el navegador) o `real` (proxy hacia `vigia-api`); ver `config/build.ts`. */
  readonly VITE_API_MODE?: 'mock' | 'real';
  /** `app_version` de la construcción (etiqueta de release o sha; `dev`); ver `vite.config.ts`. */
  readonly VITE_APP_VERSION: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

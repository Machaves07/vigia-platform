/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `mock` (MSW en el navegador) o `real` (proxy hacia `vigia-api`); ver `config/build.ts`. */
  readonly VITE_API_MODE?: 'mock' | 'real';
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

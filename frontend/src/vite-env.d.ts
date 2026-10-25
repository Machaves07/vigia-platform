/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `mock` (MSW en el navegador) o `real` (proxy hacia `vigia-api`); ver `config/build.ts`. */
  readonly VITE_API_MODE?: 'mock' | 'real';
  /** `app_version` de la construcción (etiqueta de release o sha; `dev`); ver `vite.config.ts`. */
  readonly VITE_APP_VERSION: string;
  /** Etiqueta real de @vigia/contracts, derivada del paquete en la construcción. */
  readonly VITE_CONTRACT_TAG: string;
  /** Región pública para el alta de plantas; sin valor, el alta falla cerrada (NFR-APP-35). */
  readonly VITE_PLANT_DATA_REGION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

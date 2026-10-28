/**
 * Construcción de la aplicación (LC-APP-29), base de VIG-277 ampliada por VIG-288 (TASK-411) para
 * la imagen de `vigia-platform`.
 *
 * - Salida estática con nombres con hash de contenido bajo `assets/`, sin mapas de fuente dentro
 *   de `dist/` y sin `eval` (sin CSS en JavaScript ni plantillas evaluadas; NFR-APP-14, 19).
 * - Una porción de código por pantalla `src/pages/scrNN` (NFR-APP-01, 02) y otra, `vendor`, con
 *   React, React Router y TanStack Query (VIG-355).
 * - Arranque (VIG-289): `index.html` carga `src/bootstrap/check.ts` (comprobación de capacidades)
 *   y el complemento `vigia-bootstrap` escribe la versión mínima de `browserslist` y añade el
 *   `modulepreload` del paquete inicial.
 * - Release (`config/release.ts`): `app_version` incrustada (`VITE_APP_VERSION`, etiqueta de
 *   release o sha; `dev` sin valor), `version.json` `{app_version, contract_tag, built_at}`, `.br`
 *   y `.gz` junto a cada recurso, mapas de fuente (ocultos: sin `sourceMappingURL`) en
 *   `../frontend-sourcemaps/` (o `VIGIA_SOURCEMAPS_DIR`) y fallo si `index.html` no contiene
 *   `vigia-app` (R-APP-7). Sin trabajador de servicio ni manifiesto (PAT-APP-MAN-06).
 * - Servidor de desarrollo: con `VITE_API_MODE=real` reenvía a `VITE_API_PROXY` (por omisión
 *   `vigia-api` de `make run`) toda petición que no sea de navegación (D-3); con `mock` no hay
 *   proxy y responde MSW en el navegador (VIG-286).
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, type ProxyOptions } from 'vite';

import {
  builtAt,
  contractTagFrom,
  isNavigationRequest,
  isViteOwnedPath,
  parseApiMode,
  parseApiProxy,
  resolveAppVersion,
  screenChunkName,
  screenTextsChunkName,
  vendorChunkName,
} from './config/build.ts';
import { vigiaBootstrap } from './config/bootstrap.ts';
import { vigiaRelease } from './config/release.ts';
import { systemClock } from './src/state/clock.ts';

interface PackageJson {
  readonly dependencies?: Readonly<Record<string, string>>;
}

function apiProxy(target: string): ProxyOptions {
  return {
    target,
    changeOrigin: false,
    bypass(request) {
      const url = request.url ?? '/';
      if (isViteOwnedPath(url)) return url;
      if (isNavigationRequest(request.headers)) return '/index.html';
      return undefined;
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const apiMode = parseApiMode(env.VITE_API_MODE);
  const appVersion = resolveAppVersion(env.VITE_APP_VERSION);
  const packageJson = JSON.parse(
    readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
  ) as PackageJson;
  const release = {
    app_version: appVersion,
    contract_tag: contractTagFrom(packageJson.dependencies?.['@vigia/contracts']),
    built_at: builtAt(process.env.SOURCE_DATE_EPOCH, systemClock),
  };
  const sourcemapDir =
    process.env.VIGIA_SOURCEMAPS_DIR ??
    fileURLToPath(new URL('../frontend-sourcemaps', import.meta.url));
  const root = fileURLToPath(new URL('.', import.meta.url));
  return {
    plugins: [react(), vigiaBootstrap({ root }), vigiaRelease({ release, sourcemapDir })],
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify(appVersion),
      'import.meta.env.VITE_CONTRACT_TAG': JSON.stringify(release.contract_tag),
    },
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      fs: {
        // SCR-03 distribuye el mismo archivo cuyo hash publica el backend, sin duplicarlo.
        allow: [
          root,
          fileURLToPath(new URL('../backend/tools/vigia_verify.py', import.meta.url)),
          `${fileURLToPath(new URL('../backend/tools/vigia_verify.py', import.meta.url))}?url`,
        ],
      },
      port: 5173,
      strictPort: true,
      proxy: apiMode === 'real' ? { '/': apiProxy(parseApiProxy(env.VITE_API_PROXY)) } : {},
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      // Mapas sin comentario `sourceMappingURL`: `vigia-release` los saca de `dist/`.
      sourcemap: 'hidden',
      assetsDir: 'assets',
      rolldownOptions: {
        output: {
          entryFileNames: 'assets/[name]-[hash].js',
          chunkFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
          // Equivalente en Rolldown del `manualChunks` de Rollup (obsoleto en Vite 8). Sin
          // arrastrar dependencias: un módulo compartido por varias pantallas no cae en la
          // porción de la primera.
          codeSplitting: {
            includeDependenciesRecursively: false,
            groups: [
              { name: screenChunkName, debugName: 'screens' },
              { name: screenTextsChunkName, debugName: 'screen-texts' },
              { name: vendorChunkName, debugName: 'vendor' },
            ],
          },
        },
      },
    },
  };
});

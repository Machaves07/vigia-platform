/**
 * Construcción base de la aplicación (LC-APP-29). VIG-288 (TASK-411) la amplía para la imagen:
 * `build.sourcemap` hacia `../frontend-sourcemaps`, `app_version` y `version.json`.
 *
 * - Salida estática con nombres con hash de contenido bajo `assets/`, sin mapas de fuente dentro
 *   de `dist/` y sin `eval` (sin CSS en JavaScript ni plantillas evaluadas; NFR-APP-14, 19).
 * - Una porción de código por pantalla `src/pages/scrNN` (NFR-APP-01, 02).
 * - Servidor de desarrollo: con `VITE_API_MODE=real` reenvía a `vigia-api` toda petición que no
 *   sea de navegación (D-3); con `mock` no hay proxy y responde MSW en el navegador (VIG-286).
 */
import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, type ProxyOptions } from 'vite';

import {
  DEV_API_TARGET,
  isNavigationRequest,
  isViteOwnedPath,
  parseApiMode,
  screenChunkName,
} from './config/build.ts';

const apiProxy: ProxyOptions = {
  target: DEV_API_TARGET,
  changeOrigin: false,
  bypass(request) {
    const url = request.url ?? '/';
    if (isViteOwnedPath(url)) return url;
    if (isNavigationRequest(request.headers)) return '/index.html';
    return undefined;
  },
};

export default defineConfig(({ mode }) => {
  const apiMode = parseApiMode(loadEnv(mode, process.cwd(), 'VITE_').VITE_API_MODE);
  return {
    plugins: [react()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      port: 5173,
      strictPort: true,
      proxy: apiMode === 'real' ? { '/': apiProxy } : {},
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,
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
            groups: [{ name: screenChunkName, debugName: 'screens' }],
          },
        },
      },
    },
  };
});

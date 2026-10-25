/**
 * Pruebas de la unidad (NFR-APP-43, 44): jsdom, Testing Library, `vitest-axe`, MSW con
 * `onUnhandledRequest: 'error'`, cobertura v8 y perfiles de fast-check con semilla.
 *
 * `npm test` usa el perfil `ci`; `npm run test:nightly` (`--mode nightly`) o `FC_PROFILE=nightly`
 * usan el nocturno. La semilla se resuelve una vez por corrida, `tests/setup/global.ts` la
 * imprime al empezar y `FC_SEED=<semilla>` la repite.
 */
import { fileURLToPath, URL } from 'node:url';
import { readFileSync } from 'node:fs';

import { defineConfig } from 'vitest/config';

import { resolveProfile } from './config/fastCheckProfile.ts';
import { contractTagFrom, resolveAppVersion } from './config/build.ts';

export default defineConfig(({ mode }) => {
  const profile = resolveProfile(process.env, mode === 'nightly' ? 'nightly' : 'ci');
  const packageJson = JSON.parse(
    readFileSync(new URL('./package.json', import.meta.url), 'utf8'),
  ) as { dependencies: Record<string, string> };
  return {
    server: {
      fs: {
        allow: [
          fileURLToPath(new URL('.', import.meta.url)),
          fileURLToPath(new URL('../backend/tools/vigia_verify.py', import.meta.url)),
          `${fileURLToPath(new URL('../backend/tools/vigia_verify.py', import.meta.url))}?url`,
        ],
      },
    },
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    test: {
      environment: 'jsdom',
      include: ['tests/**/*.test.{ts,tsx}'],
      exclude: ['tests/e2e/**', 'node_modules/**'],
      globalSetup: ['tests/setup/global.ts'],
      setupFiles: ['tests/setup/index.ts'],
      env: {
        // Misma construcción sintética para el controlador y MSW, sin depender del entorno local.
        VITE_APP_VERSION: resolveAppVersion(undefined),
        VITE_CONTRACT_TAG: contractTagFrom(packageJson.dependencies['@vigia/contracts']),
        FC_PROFILE: profile.name,
        FC_SEED: String(profile.seed),
      },
      restoreMocks: true,
      unstubEnvs: true,
      unstubGlobals: true,
      coverage: {
        provider: 'v8',
        include: ['src/**/*.{ts,tsx}'],
        reporter: ['text-summary', 'lcov'],
        reportsDirectory: 'coverage',
        // NFR-APP-46 `[objetivo propio]`: ≥ 80 % de líneas en el estado, los puertos, los
        // validadores, el mapa de errores y la composición de cobertura, cada área por separado.
        // Solo se aplica con `--coverage` (el trabajo `frontend` de `ci.yml`; VIG-294).
        thresholds: {
          'src/state/**': { lines: 80 },
          'src/ports/**': { lines: 80 },
          'src/ports/validation/**': { lines: 80 },
          'src/ports/errors/**': { lines: 80 },
          'src/state/coverage/**': { lines: 80 },
        },
      },
    },
  };
});

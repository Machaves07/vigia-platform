/**
 * Pruebas de la unidad (NFR-APP-43, 44): jsdom, Testing Library, `vitest-axe`, MSW con
 * `onUnhandledRequest: 'error'`, cobertura v8 y perfiles de fast-check con semilla.
 *
 * `npm test` usa el perfil `ci`; `npm run test:nightly` (`--mode nightly`) o `FC_PROFILE=nightly`
 * usan el nocturno. La semilla se resuelve una vez por corrida, `tests/setup/global.ts` la
 * imprime al empezar y `FC_SEED=<semilla>` la repite.
 */
import { fileURLToPath, URL } from 'node:url';

import { defineConfig } from 'vitest/config';

import { resolveProfile } from './config/fastCheckProfile.ts';

export default defineConfig(({ mode }) => {
  const profile = resolveProfile(process.env, mode === 'nightly' ? 'nightly' : 'ci');
  return {
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
      },
    },
  };
});

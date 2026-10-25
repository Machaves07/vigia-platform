/**
 * Regresiones nativas de LC-APP-01 (VIG-343) y de la CSP estricta con `<select>` (VIG-345);
 * independientes de los recorridos por rol de VIG-299.
 */
import { defineConfig } from '@playwright/test';

import { compatibleWebkitExecutable } from './webkit.ts';

export default defineConfig({
  testDir: '.',
  testMatch: ['bootstrap_h264.spec.ts', 'csp_select.spec.ts'],
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  outputDir: '../../test-results/bootstrap',
  use: { ignoreHTTPSErrors: true, trace: 'off', screenshot: 'off', video: 'off' },
  projects: [
    // El agente nativo HeadlessChrome no pertenece a la política de producción. Usar Xvfb
    // si no hay pantalla, nunca falsificar navigator.userAgent para superar la guarda.
    { name: 'chromium', use: { browserName: 'chromium', headless: false } },
    { name: 'firefox', use: { browserName: 'firefox' } },
    // Compilación fijada por VIG-345 (`node scripts/install_webkit.mjs`), no la de la caché.
    {
      name: 'webkit',
      use: {
        browserName: 'webkit',
        launchOptions: { executablePath: compatibleWebkitExecutable() },
      },
    },
    {
      name: 'firefox-no-h264',
      use: {
        browserName: 'firefox',
        launchOptions: {
          // Desactiva los decodificadores nativos, no la API ni su respuesta.
          firefoxUserPrefs: {
            'media.ffmpeg.enabled': false,
            'media.gmp.decoder.enabled': false,
          },
        },
      },
    },
  ],
});

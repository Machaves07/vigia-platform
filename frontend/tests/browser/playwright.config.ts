/** Regresión nativa de LC-APP-01; independiente de los recorridos por rol de VIG-299. */
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'bootstrap_h264.spec.ts',
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
    { name: 'webkit', use: { browserName: 'webkit' } },
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

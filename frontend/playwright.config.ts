import { defineConfig } from '@playwright/test';

import { compatibleWebkitExecutable } from './tests/browser/webkit';

const engines = ['chromium', 'firefox', 'webkit'] as const;
// Alternativa para anfitriones sin bibliotecas de WebKit: servidor local en la imagen oficial.
const webkitEndpoint = process.env.VIGIA_WEBKIT_WS_ENDPOINT;
const sizes = [
  { name: 'desktop', viewport: { width: 1280, height: 800 }, hasTouch: false },
  { name: 'tablet', viewport: { width: 768, height: 1024 }, hasTouch: true },
] as const;

export default defineConfig({
  testDir: './tests/e2e',
  // Este recorrido recibe credenciales efímeras únicamente desde la comprobación de despliegue.
  testIgnore: 'deploy_check_staging.spec.ts',
  globalSetup: './tests/e2e/global-setup.ts',
  globalTeardown: './tests/e2e/global-teardown.ts',
  // Una plataforma por ejecución y un usuario por rol: no competir por el mismo paso TOTP.
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  use: {
    ignoreHTTPSErrors: true, // Solo la CA efímera local; no se desactiva CSP.
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: engines.flatMap((browserName) =>
    sizes.map(({ name, viewport, hasTouch }) => ({
      name: `${browserName}-${name}`,
      use: {
        browserName,
        viewport,
        hasTouch,
        // Chromium gráfico conserva su identificación nativa, sin simular el agente de usuario.
        ...(browserName === 'chromium' ? { headless: false } : {}),
        // VIG-345: la revisión 2370 genera una violación nativa al crear select.
        ...(browserName === 'webkit'
          ? webkitEndpoint
            ? { connectOptions: { wsEndpoint: webkitEndpoint, exposeNetwork: '<loopback>' } }
            : { launchOptions: { executablePath: compatibleWebkitExecutable() } }
          : {}),
      },
    })),
  ),
});

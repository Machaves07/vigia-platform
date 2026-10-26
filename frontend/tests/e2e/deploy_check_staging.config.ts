import { defineConfig } from '@playwright/test';

const target = process.env.VIGIA_DEPLOY_CHECK_TARGET;
const credentials = process.env.VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE;

// Solo en local (--ca-file): huella de la clave que la raíz efímera ya verificó en Python.
const pinnedKey = process.env.VIGIA_DEPLOY_CHECK_SPKI;

if (!target || !credentials) {
  throw new Error('El recorrido de despliegue requiere destino y archivo privado de credenciales');
}
if (pinnedKey !== undefined && !/^[A-Za-z0-9+/]{43}=$/.test(pinnedKey)) {
  throw new Error('La huella de la clave del destino local no es un SHA-256 en base64');
}

export default defineConfig({
  testDir: '.',
  testMatch: 'deploy_check_staging.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  use: {
    baseURL: target,
    ignoreHTTPSErrors: false,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  projects: [
    {
      name: 'deploy-check',
      use: {
        browserName: 'chromium',
        // Sin relajar TLS: Chromium acepta solo el certificado cuya clave coincide.
        ...(pinnedKey
          ? { launchOptions: { args: [`--ignore-certificate-errors-spki-list=${pinnedKey}`] } }
          : {}),
      },
    },
  ],
});

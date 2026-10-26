import { defineConfig } from '@playwright/test';

const target = process.env.VIGIA_DEPLOY_CHECK_TARGET;
const credentials = process.env.VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE;

if (!target || !credentials) {
  throw new Error('El recorrido de despliegue requiere destino y archivo privado de credenciales');
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
  projects: [{ name: 'deploy-check', use: { browserName: 'chromium' } }],
});

import { afterEach, describe, expect, it, vi } from 'vitest';

const PIN = `${'A'.repeat(43)}=`;

async function load(environment: Record<string, string>) {
  vi.stubEnv('VIGIA_DEPLOY_CHECK_TARGET', 'https://127.0.0.1:4443');
  vi.stubEnv('VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE', '/tmp/vigia-deploy-check-sintetico.json');
  for (const [name, value] of Object.entries(environment)) vi.stubEnv(name, value);
  vi.resetModules();
  return (await import('../../e2e/deploy_check_staging.config')).default;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

// La primera importación de @playwright/test tarda varios segundos: el tope no decide nada.
describe('recorrido de la comprobación de despliegue nº 8', { timeout: 60_000 }, () => {
  it('usa Chromium gráfico con su identificación nativa y sin artefactos', async () => {
    const config = await load({});
    const [project] = config.projects ?? [];
    expect(config.projects).toHaveLength(1);
    expect(project?.use?.browserName).toBe('chromium');
    expect(project?.use?.headless).toBe(false);
    expect(project?.use?.userAgent).toBeUndefined();
    expect(config.use?.ignoreHTTPSErrors).toBe(false);
    expect(project?.use?.ignoreHTTPSErrors).toBeUndefined();
    expect([config.use?.trace, config.use?.video, config.use?.screenshot]).toEqual([
      'off',
      'off',
      'off',
    ]);
  });

  it('sin huella no cambia los argumentos de Chromium', async () => {
    const [project] = (await load({})).projects ?? [];
    expect(project?.use?.launchOptions).toBeUndefined();
  });

  it('con huella acepta solo esa clave del destino local', async () => {
    const [project] = (await load({ VIGIA_DEPLOY_CHECK_SPKI: PIN })).projects ?? [];
    expect(project?.use?.launchOptions).toEqual({
      args: [`--ignore-certificate-errors-spki-list=${PIN}`],
    });
  });

  it.each([
    ['vacía', ''],
    ['corta', 'A'.repeat(43)],
    ['con lista', `${PIN},${PIN}`],
    ['con argumento', `${PIN} --ignore-certificate-errors`],
  ])('rechaza una huella %s', async (_label, value) => {
    await expect(load({ VIGIA_DEPLOY_CHECK_SPKI: value })).rejects.toThrow(/huella/);
  });

  it('exige destino y archivo privado de credenciales', async () => {
    vi.stubEnv('VIGIA_DEPLOY_CHECK_TARGET', '');
    vi.resetModules();
    await expect(import('../../e2e/deploy_check_staging.config')).rejects.toThrow(/destino/);
  });
});

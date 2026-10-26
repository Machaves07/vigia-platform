import { afterEach, describe, expect, it, vi } from 'vitest';

import config from '../../../playwright.config';
import { compatibleWebkitExecutable } from '../../browser/webkit';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('matriz real de Playwright', () => {
  const projects = config.projects ?? [];

  it('declara tres motores por los dos tamaños y tacto solo en tableta', () => {
    expect(projects.map((project) => project.name).sort()).toEqual([
      'chromium-desktop',
      'chromium-tablet',
      'firefox-desktop',
      'firefox-tablet',
      'webkit-desktop',
      'webkit-tablet',
    ]);
    for (const project of projects) {
      expect(project.use?.browserName).toBe(project.name?.split('-')[0]);
      const tablet = project.name?.endsWith('-tablet');
      expect(project.use?.viewport).toEqual(
        tablet ? { width: 768, height: 1024 } : { width: 1280, height: 800 },
      );
      expect(project.use?.hasTouch).toBe(tablet);
    }
  });

  it('conserva la identificación nativa y fija el WebKit compatible con CSP', () => {
    for (const project of projects) {
      expect(project.use?.userAgent).toBeUndefined();
      if (project.use?.browserName === 'chromium') expect(project.use.headless).toBe(false);
      if (project.use?.browserName === 'webkit') {
        expect(project.use.launchOptions?.executablePath).toBe(compatibleWebkitExecutable());
      }
    }
  });

  it('solo WebKit puede usar el servidor alternativo con red de bucle local', async () => {
    vi.stubEnv('VIGIA_WEBKIT_WS_ENDPOINT', 'ws://127.0.0.1:9325/vigia-e2e-webkit');
    vi.resetModules();
    const { default: remote } = await import('../../../playwright.config');
    for (const project of remote.projects ?? []) {
      if (project.use?.browserName === 'webkit') {
        expect(project.use.connectOptions).toEqual({
          wsEndpoint: 'ws://127.0.0.1:9325/vigia-e2e-webkit',
          exposeNetwork: '<loopback>',
        });
        expect(project.use.launchOptions).toBeUndefined();
      } else expect(project.use?.connectOptions).toBeUndefined();
    }
  });
});

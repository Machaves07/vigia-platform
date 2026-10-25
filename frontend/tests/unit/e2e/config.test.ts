import { describe, expect, it } from 'vitest';

import config from '../../../playwright.config';
import { compatibleWebkitExecutable } from '../../browser/webkit';

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
});

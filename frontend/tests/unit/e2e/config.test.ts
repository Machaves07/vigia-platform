import { describe, expect, it } from 'vitest';

import config from '../../../playwright.config';

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

  it('usa el perfil Chrome oficial, sin el identificador HeadlessChrome ajeno a la política', () => {
    for (const project of projects.filter((item) => item.use?.browserName === 'chromium')) {
      expect(project.use?.userAgent).toMatch(/ Chrome\/\d+/);
      expect(project.use?.userAgent).not.toContain('HeadlessChrome');
    }
  });
});

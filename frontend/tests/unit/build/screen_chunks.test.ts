// @vitest-environment node
/**
 * Una porción por pantalla (NFR-APP-01, 02; PAT-APP-REN-02, VIG-290): `vite build` con la
 * configuración real produce dieciséis `assets/scrNN-<hash>.js`, y ni `index.html` ni el paquete
 * inicial cargan ninguna de forma estática: solo se piden al navegar o al precargar el inicio.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
const outDir = mkdtempSync(join(tmpdir(), 'vigia-chunks-'));

afterAll(() => {
  rmSync(outDir, { recursive: true, force: true });
});

describe('porciones de pantalla en dist/', () => {
  it('dieciséis scrNN y ninguna importada de forma estática por el arranque', () => {
    const env: Record<string, string | undefined> = { ...process.env };
    delete env.NODE_ENV;
    const result = spawnSync(
      process.execPath,
      [VITE, 'build', '--outDir', outDir, '--emptyOutDir'],
      { cwd: ROOT, env, encoding: 'utf8' },
    );
    expect(result.status, result.stderr).toBe(0);

    const assets = readdirSync(join(outDir, 'assets'));
    const screens = assets
      .map((name) => /^(scr\d{2})-[A-Za-z0-9_-]+\.js$/.exec(name)?.[1])
      .filter((name) => name !== undefined)
      .sort();
    expect(screens).toEqual(
      Array.from({ length: 16 }, (_, i) => `scr${String(i + 1).padStart(2, '0')}`),
    );

    // React, React Router y TanStack Query en una sola porción (VIG-355, NFR-APP-01).
    expect(assets.filter((name) => /^vendor-[A-Za-z0-9_-]+\.js$/.test(name))).toHaveLength(1);

    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    expect(html).not.toMatch(/scr\d{2}-/);
    const entries = assets.filter((name) => /^index-.*\.js$/.test(name));
    expect(entries).toHaveLength(1);
    const entry = readFileSync(join(outDir, 'assets', entries[0] ?? ''), 'utf8');
    expect(entry).not.toMatch(/(?:import|from)\s*["']\.\/scr\d{2}-/);
  }, 120_000);
});

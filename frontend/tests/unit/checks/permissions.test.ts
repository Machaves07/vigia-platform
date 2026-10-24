// @vitest-environment node
/**
 * `scripts/checks/permissions.mjs` (BR-APP-02): pasa con los descriptores reales y falla con una
 * clave inventada, una clave de la plataforma, un rol inexistente o una lista que no es literal.
 */
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const CHECK = join(ROOT, 'scripts', 'checks', 'permissions.mjs');
const REAL = readFileSync(join(ROOT, 'src', 'pages', 'scr10', 'descriptor.ts'), 'utf8');

const temporary = mkdtempSync(join(tmpdir(), 'vigia-permissions-'));
afterAll(() => {
  rmSync(temporary, { recursive: true, force: true });
});

let counter = 0;
function run(descriptor?: string) {
  if (descriptor === undefined) return spawnSync(process.execPath, [CHECK], { encoding: 'utf8' });
  counter += 1;
  const pages = join(temporary, String(counter));
  mkdirSync(join(pages, 'scr10'), { recursive: true });
  writeFileSync(join(pages, 'scr10', 'descriptor.ts'), descriptor);
  return spawnSync(process.execPath, [CHECK, pages], { encoding: 'utf8' });
}

describe('permissions.mjs', () => {
  it('pasa con los dieciséis descriptores reales', () => {
    const result = run();
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('Descriptores de pantalla: 16');
  });

  it('pasa con una copia del descriptor real', () => {
    expect(run(REAL).status).toBe(0);
  });

  it.each([
    ['clave inventada', "'review_queue.resolve'", "'loop.manage'", 'loop.manage'],
    ['clave de la plataforma', "'review_queue.resolve'", "'platform.keys.rotate'", 'plataforma'],
    ['rol inexistente', "'coordinator_sst'", "'supervisor'", 'supervisor'],
    [
      'lista que no es literal',
      "required_permissions: ['findings.read', 'review_queue.resolve']",
      'required_permissions: KEYS',
      'lista literal',
    ],
    ['elemento que no es texto', "'review_queue.resolve'", 'KEY', 'textos literales'],
  ])('falla con %s', (_name, from, to, message) => {
    const mutated = REAL.replace(from, to);
    expect(mutated).not.toBe(REAL);
    const result = run(mutated);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(message);
  });
});

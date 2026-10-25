// @vitest-environment node
/**
 * `scripts/checks/licenses.mjs` (NFR-APP-25, A-43): pasa con el `package-lock.json` real y, sobre
 * bloqueos sintéticos, falla con una licencia fuera de la lista, una expresión SPDX que no vale o
 * no se entiende, un paquete sin licencia, una excepción de desarrollo en producción y una
 * excepción vencida.
 */
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const CHECK = join(ROOT, 'scripts', 'checks', 'licenses.mjs');

const temporary = mkdtempSync(join(tmpdir(), 'vigia-licenses-'));
afterAll(() => {
  rmSync(temporary, { recursive: true, force: true });
});

interface Entry {
  readonly version?: string;
  readonly license?: string;
  readonly dev?: boolean;
  readonly installed?: Record<string, unknown>;
}

let counter = 0;
function run(entries: Record<string, Entry>, ...options: string[]) {
  counter += 1;
  const directory = join(temporary, String(counter));
  mkdirSync(directory, { recursive: true });
  const packages: Record<string, unknown> = { '': { name: 'raiz', version: '0.0.0' } };
  for (const [name, { installed, ...entry }] of Object.entries(entries)) {
    const path = `node_modules/${name}`;
    packages[path] = { version: '1.0.0', ...entry };
    if (installed !== undefined) {
      mkdirSync(join(directory, path), { recursive: true });
      writeFileSync(join(directory, path, 'package.json'), JSON.stringify(installed));
    }
  }
  const lock = join(directory, 'package-lock.json');
  writeFileSync(lock, JSON.stringify({ lockfileVersion: 3, packages }));
  return spawnSync(process.execPath, [CHECK, lock, '--today', '2026-10-25', ...options], {
    encoding: 'utf8',
  });
}

describe('licenses.mjs', () => {
  it('pasa con el package-lock.json real', () => {
    const result = spawnSync(process.execPath, [CHECK], { encoding: 'utf8' });
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(result.stdout).toMatch(/^Licencias permitidas: \d+ paquetes/);
  });

  it('pasa con la lista permitida y con expresiones SPDX que la cumplen', () => {
    const result = run({
      a: { license: 'MIT' },
      b: { license: 'Apache-2.0' },
      c: { license: '(MIT OR GPL-3.0-only)' },
      d: { license: 'MIT AND ISC' },
      e: { license: 'Apache-2.0 WITH LLVM-exception' },
      f: { license: 'Python-2.0', dev: true },
      g: { license: 'BlueOak-1.0.0' },
      h: { license: 'CC-BY-4.0', dev: true },
      '@vigia/contracts': { license: 'SEE LICENSE IN LICENSE' },
    });
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('Licencias permitidas: 9 paquetes');
  });

  it.each([
    ['GPL-3.0-only', false, 'no permitida (producción'],
    ['GPL-3.0-only', true, 'no permitida (desarrollo'],
    ['MIT AND GPL-3.0-only', false, 'no permitida'],
    ['CC-BY-4.0', false, 'no permitida (producción'],
    ['CC-BY-3.0', false, 'no permitida (producción'],
    ['SEE LICENSE IN LICENSE', false, 'no es una expresión SPDX'],
    ['UNLICENSED', false, 'no permitida'],
    ['MIT OR', false, 'no es una expresión SPDX'],
    ['(MIT', false, 'no es una expresión SPDX'],
    ['MIT ISC', false, 'no es una expresión SPDX'],
  ])('falla con «%s» (dev: %s)', (license, dev, message) => {
    const result = run({ ok: { license: 'MIT' }, malo: { license, dev } });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`malo@1.0.0: licencia «${license}»`);
    expect(result.stderr).toContain(message);
    expect(result.stderr).not.toContain('ok@');
  });

  it('falla con un paquete sin licencia en el bloqueo ni instalada', () => {
    const result = run({ mudo: {} });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('mudo@1.0.0: sin licencia declarada');
  });

  it('lee la licencia instalada, también el formato antiguo, que exige todas sus entradas', () => {
    expect(run({ a: { installed: { license: 'ISC' } } }).status).toBe(0);
    expect(run({ a: { installed: { licenses: [{ type: 'BSD' }] } } }).status).toBe(0);
    const both = run({ a: { installed: { licenses: [{ type: 'MIT' }, { type: 'GPL-2.0' }] } } });
    expect(both.status).toBe(1);
    expect(both.stderr).toContain('licencia «(MIT AND GPL-2.0)»');
  });

  it('falla con una excepción en uso cuya fecha de revisión pasó', () => {
    expect(run({ a: { license: 'MIT-0' } }, '--today', '2027-03-31').status).toBe(0);
    const result = run({ a: { license: 'MIT-0' } }, '--today', '2027-04-01');
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('(MIT-0) vencida el 2027-03-31');
    // Una excepción que nadie usa no falla al vencer.
    expect(run({ a: { license: 'MIT' } }, '--today', '2030-01-01').status).toBe(0);
  });

  it('termina en 2 si no puede leer el bloqueo', () => {
    const result = spawnSync(process.execPath, [CHECK, join(temporary, 'no-existe.json')], {
      encoding: 'utf8',
    });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('no se pudo leer');
  });
});

// @vitest-environment node
/**
 * Agregadores por descubrimiento de `tests/views/`, `tests/msw/scenarios/` y `scripts/checks/`:
 * con cero archivos pasan, y dos archivos con el mismo nombre o una comprobación que falla
 * detienen la carga.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, describe, expect, it } from 'vitest';

import { scenarios } from '../msw/scenarios';
import { aggregateModules } from '../support/aggregate';
import { views } from '../views';

const RUN = fileURLToPath(new URL('../../scripts/checks/run.mjs', import.meta.url));
const directories: string[] = [];

function checksDirectory(files: Readonly<Record<string, string>>): string {
  const directory = mkdtempSync(join(tmpdir(), 'vigia-checks-'));
  directories.push(directory);
  for (const [name, source] of Object.entries(files)) writeFileSync(join(directory, name), source);
  return directory;
}

function runChecks(directory?: string) {
  const args = directory === undefined ? [RUN] : [RUN, directory];
  return spawnSync(process.execPath, args, { encoding: 'utf8' });
}

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true });
});

describe('tests/views y tests/msw/scenarios', () => {
  it('con cero archivos el agregado está vacío', () => {
    expect(views).toEqual({});
    expect(scenarios).toEqual({});
  });

  it('reúne módulos distintos y rechaza un nombre repetido', () => {
    expect(aggregateModules('x', { './a.ts': { one: 1 }, './b.ts': { two: 2 } })).toEqual({
      one: 1,
      two: 2,
    });
    expect(() => aggregateModules('x', { './a.ts': { one: 1 }, './b.ts': { one: 2 } })).toThrow(
      'x: «one» repetido en ./a.ts y ./b.ts.',
    );
    expect(() => aggregateModules('x', { './a.ts': [1] })).toThrow('x: ./a.ts');
  });
});

describe('scripts/checks/run.mjs', () => {
  it('pasa con las comprobaciones del repositorio (hoy, ninguna)', () => {
    const result = runChecks();
    expect(result.status, result.stderr).toBe(0);
  });

  it('pasa con una carpeta vacía', () => {
    const result = runChecks(checksDirectory({}));
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('Comprobaciones superadas: 0.');
  });

  it('ejecuta todas en orden y falla si alguna falla', () => {
    const directory = checksDirectory({
      'b-fails.mjs': "process.stdout.write('b\\n'); process.exit(3);",
      'a-passes.mjs': "process.stdout.write('a\\n');",
      'c-passes.mjs': "process.stdout.write('c\\n');",
      'notes.txt': 'no es una comprobación',
    });
    const result = runChecks(directory);
    expect(result.status).toBe(1);
    expect(result.stdout.match(/^[abc]$/gm)).toEqual(['a', 'b', 'c']);
    expect(result.stderr).toContain('Comprobaciones fallidas (1): b-fails.mjs');
  });
});

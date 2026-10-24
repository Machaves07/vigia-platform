// @vitest-environment node
/**
 * `npm run generate:check` (NFR-APP-48, VIG-280): lo versionado en `src/client/` y
 * `openapi/bodies/` es exactamente lo que genera `backend/openapi/app.yaml`; un archivo generado
 * editado a mano, que falta o que sobra pone la comprobación en rojo con la diferencia.
 */
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeAll, describe, expect, it } from 'vitest';

import { compareOutputs } from '../../../scripts/generate.mjs';
import { renderAll, SPEC_PATH } from '../../../scripts/generate/render.mjs';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const spec = join(root, SPEC_PATH);
const LONG = 120_000;

let files: Map<string, string>;
const scratch: string[] = [];

beforeAll(async () => {
  files = await renderAll(readFileSync(spec, 'utf8'));
}, LONG);

afterEach(() => {
  for (const directory of scratch.splice(0)) rmSync(directory, { recursive: true, force: true });
});

/** Copia de lo generado en una carpeta temporal, como si estuviera versionado. */
function copyOutputs(): string {
  const directory = mkdtempSync(join(tmpdir(), 'vig-280-generate-'));
  scratch.push(directory);
  for (const [name, content] of files) {
    mkdirSync(dirname(join(directory, name)), { recursive: true });
    writeFileSync(join(directory, name), content);
  }
  return directory;
}

/** Sustituye la primera línea que contiene `needle` y devuelve la original y la nueva. */
function editLine(file: string, needle: string, change: (line: string) => string) {
  const lines = readFileSync(file, 'utf8').split('\n');
  const index = lines.findIndex((line) => line.includes(needle));
  expect(index).toBeGreaterThanOrEqual(0);
  const before = lines[index] ?? '';
  const after = change(before);
  lines[index] = after;
  writeFileSync(file, lines.join('\n'));
  return { before, after, line: index + 1 };
}

describe('generate:check', () => {
  it('lo versionado coincide con lo que genera app.yaml', () => {
    expect(compareOutputs(root, files)).toEqual([]);
  });

  it(
    'genera los mismos bytes dos veces (reproducible)',
    async () => {
      const again = await renderAll(readFileSync(spec, 'utf8'));
      expect([...again.keys()]).toEqual([...files.keys()]);
      for (const [name, content] of again) expect(content, name).toBe(files.get(name));
    },
    LONG,
  );

  it('cada archivo generado lleva la cabecera «no se edita a mano»', () => {
    for (const [name, content] of files) {
      expect(content.slice(0, 600), name).toContain('no se edita a mano');
    }
  });

  it('una línea de src/client/schema.d.ts editada a mano da la diferencia de esa línea', () => {
    const directory = copyOutputs();
    const edited = editLine(join(directory, 'src/client/schema.d.ts'), "'/auth/login': {", (line) =>
      line.replace('/auth/login', '/auth/login-a-mano'),
    );
    expect(compareOutputs(directory, files)).toEqual([
      [
        '--- src/client/schema.d.ts (versionado)',
        '+++ src/client/schema.d.ts (generado)',
        `@@ línea ${String(edited.line)} @@`,
        ...context(join(directory, 'src/client/schema.d.ts'), edited.line),
      ].join('\n'),
    ]);
  });

  it('un límite editado a mano en un esquema de openapi/bodies/ da la diferencia', () => {
    const directory = copyOutputs();
    const name = 'openapi/bodies/login_auth_login_post.json';
    const edited = editLine(join(directory, name), '"maxLength": 254', (line) =>
      line.replace('254', '255'),
    );
    const [problem] = compareOutputs(directory, files);
    expect(problem?.split('\n').slice(0, 3)).toEqual([
      `--- ${name} (versionado)`,
      `+++ ${name} (generado)`,
      `@@ línea ${String(edited.line)} @@`,
    ]);
    expect(problem?.split('\n')).toContain(`- ${edited.before.replace('254', '255')}`);
    expect(problem?.split('\n')).toContain(`+ ${edited.before}`);
  });

  it('un esquema que sobra o que falta en openapi/bodies/ también falla', () => {
    const directory = copyOutputs();
    writeFileSync(join(directory, 'openapi/bodies/sin_operacion.json'), '{}\n');
    rmSync(join(directory, 'openapi/bodies/login_auth_login_post.json'));
    expect(compareOutputs(directory, files)).toEqual([
      'falta openapi/bodies/login_auth_login_post.json: ejecuta «npm run generate».',
      'sobra openapi/bodies/sin_operacion.json: ninguna operación de app.yaml lo genera.',
    ]);
  });

  it(
    'la orden --check sale con 1 e imprime la diferencia; sin cambios, sale con 0',
    () => {
      const directory = copyOutputs();
      const script = join(root, 'scripts/generate.mjs');
      const clean = spawnSync(
        process.execPath,
        [script, '--check', '--root', directory, '--spec', spec],
        {
          encoding: 'utf8',
        },
      );
      expect(clean.status).toBe(0);
      editLine(join(directory, 'src/client/schema.d.ts'), "'/auth/login': {", (line) => `${line} `);
      const dirty = spawnSync(
        process.execPath,
        [script, '--check', '--root', directory, '--spec', spec],
        {
          encoding: 'utf8',
        },
      );
      expect(dirty.status).toBe(1);
      expect(dirty.stderr).toContain('--- src/client/schema.d.ts (versionado)');
      expect(dirty.stderr).toContain(
        'generate:check: 1 archivo(s) generado(s) no coinciden con app.yaml',
      );
    },
    LONG,
  );
});

/** Contexto que imprime `lineDiff` para una sola línea cambiada en `line` (1 = primera). */
function context(file: string, line: number): string[] {
  const current = readFileSync(file, 'utf8').split('\n');
  const expected = (files.get('src/client/schema.d.ts') ?? '').split('\n');
  const index = line - 1;
  return [
    ...current.slice(Math.max(0, index - 3), index).map((text) => `  ${text}`),
    `- ${current[index] ?? ''}`,
    `+ ${expected[index] ?? ''}`,
    ...current.slice(index + 1, index + 4).map((text) => `  ${text}`),
  ];
}

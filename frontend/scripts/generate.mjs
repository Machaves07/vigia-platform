#!/usr/bin/env node
/**
 * `npm run generate` y `npm run generate:check` (LC-APP-12 y 16, NFR-APP-48, BR-APP-63).
 *
 * Lee `../backend/openapi/app.yaml` y escribe el cliente (`src/client/`, salvo `assumed.ts`, que
 * es a mano) y los esquemas espejo (`openapi/bodies/`); ver `scripts/generate/render.mjs`. Con
 * `--check` no escribe: compara con lo versionado, imprime la diferencia de cada archivo distinto,
 * que falta o que sobra, y falla. Cualquier PR que cambie `app.yaml` regenera en el mismo PR.
 *
 * Opciones (las usan sus pruebas): `--root <dir>` (por omisión, `frontend/`) y `--spec <archivo>`
 * (por omisión, `<root>/../backend/openapi/app.yaml`).
 */
import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

import { BODIES_DIR, renderAll, SPEC_PATH } from './generate/render.mjs';

const CONTEXT = 3;
const MAX_LINES = 40;

/**
 * @param {string} file
 * @returns {string | null}
 */
function readOrNull(file) {
  try {
    return readFileSync(file, 'utf8');
  } catch {
    return null;
  }
}

/**
 * Diferencia por líneas del primer tramo distinto (`-` versionado, `+` generado).
 *
 * @param {string} name
 * @param {string} current
 * @param {string} expected
 * @returns {string}
 */
export function lineDiff(name, current, expected) {
  const before = current.split('\n');
  const after = expected.split('\n');
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start])
    start += 1;
  let endBefore = before.length;
  let endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) {
    endBefore -= 1;
    endAfter -= 1;
  }
  const from = Math.max(0, start - CONTEXT);
  const lines = [
    `--- ${name} (versionado)`,
    `+++ ${name} (generado)`,
    `@@ línea ${String(start + 1)} @@`,
    ...before.slice(from, start).map((line) => `  ${line}`),
    ...before.slice(start, endBefore).map((line) => `- ${line}`),
    ...after.slice(start, endAfter).map((line) => `+ ${line}`),
    ...before.slice(endBefore, endBefore + CONTEXT).map((line) => `  ${line}`),
  ];
  if (lines.length > MAX_LINES) {
    return [
      ...lines.slice(0, MAX_LINES),
      `… (${String(lines.length - MAX_LINES)} líneas más)`,
    ].join('\n');
  }
  return lines.join('\n');
}

/**
 * Diferencias entre lo generado y lo que hay en `root`, una por archivo distinto, faltante o sobrante.
 *
 * @param {string} root
 * @param {Map<string, string>} files
 * @returns {string[]}
 */
export function compareOutputs(root, files) {
  /** @type {string[]} */
  const problems = [];
  for (const [name, expected] of files) {
    const current = readOrNull(join(root, name));
    if (current === null) problems.push(`falta ${name}: ejecuta «npm run generate».`);
    else if (current !== expected) problems.push(lineDiff(name, current, expected));
  }
  for (const name of staleBodies(root, files)) {
    problems.push(`sobra ${name}: ninguna operación de app.yaml lo genera.`);
  }
  return problems;
}

/**
 * @param {string} root
 * @param {Map<string, string>} files
 * @returns {string[]}
 */
function staleBodies(root, files) {
  let names;
  try {
    names = readdirSync(join(root, BODIES_DIR));
  } catch {
    return [];
  }
  return names
    .map((name) => `${BODIES_DIR}/${name}`)
    .filter((name) => !files.has(name))
    .sort();
}

/**
 * @param {string} root
 * @param {Map<string, string>} files
 */
function writeOutputs(root, files) {
  for (const name of staleBodies(root, files)) rmSync(join(root, name));
  for (const [name, content] of files) {
    const target = join(root, name);
    mkdirSync(dirname(target), { recursive: true });
    if (readOrNull(target) !== content) writeFileSync(target, content);
  }
}

async function main() {
  const { values } = parseArgs({
    options: {
      check: { type: 'boolean', default: false },
      root: { type: 'string' },
      spec: { type: 'string' },
    },
  });
  const root = resolve(values.root ?? join(dirname(fileURLToPath(import.meta.url)), '..'));
  const spec = resolve(values.spec ?? join(root, SPEC_PATH));
  const files = await renderAll(readFileSync(spec, 'utf8'));
  if (!values.check) {
    writeOutputs(root, files);
    process.stdout.write(`Generados ${String(files.size)} archivos desde ${spec}.\n`);
    return;
  }
  const problems = compareOutputs(root, files);
  if (problems.length > 0) {
    process.stderr.write(`${problems.join('\n\n')}\n\n`);
    process.stderr.write(
      `generate:check: ${String(problems.length)} archivo(s) generado(s) no coinciden con app.yaml; ` +
        'lo generado no se edita a mano: ejecuta «npm run generate».\n',
    );
    process.exit(1);
  }
  process.stdout.write(
    `generate:check: ${String(files.size)} archivos generados sin diferencia.\n`,
  );
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}

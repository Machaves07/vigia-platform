#!/usr/bin/env node
/**
 * `npm run knip` (NFR-APP-25): knip con `knip.json` más cada `knip.ignore.<tarea>.json`.
 *
 * El andamiaje (VIG-277) instala todo el presupuesto de dependencias aunque se use después. Lo
 * que aún no se usa queda ignorado en un archivo por tarea consumidora, que esa tarea borra al
 * empezar a usarlo: así ninguna tarea edita un archivo compartido. Cada archivo solo puede
 * aportar `ignoreDependencies`, `ignoreBinaries` e `ignore`; cualquier otra clave detiene knip.
 */
import { spawnSync } from 'node:child_process';
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MERGEABLE = ['ignoreDependencies', 'ignoreBinaries', 'ignore'];
const ALLOWED = new Set([...MERGEABLE, '$comment', 'task']);

/**
 * @param {string} name
 * @returns {Record<string, unknown>}
 */
const readJson = (name) => JSON.parse(readFileSync(join(root, name), 'utf8'));

/**
 * @param {unknown} value
 * @param {string} where
 * @returns {string[]}
 */
const strings = (value, where) => {
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) {
    process.stderr.write(`${where} debe ser una lista de textos.\n`);
    process.exit(2);
  }
  return value;
};

const config = readJson('knip.json');
const parts = readdirSync(root)
  .filter((name) => /^knip\.ignore\.[A-Za-z0-9-]+\.json$/.test(name))
  .sort();

for (const name of parts) {
  const part = readJson(name);
  for (const key of Object.keys(part)) {
    if (!ALLOWED.has(key)) {
      process.stderr.write(
        `${name}: clave «${key}» no permitida (solo ${MERGEABLE.join(', ')}).\n`,
      );
      process.exit(2);
    }
  }
  for (const key of MERGEABLE) {
    if (part[key] === undefined) continue;
    const base = config[key] === undefined ? [] : strings(config[key], `knip.json: ${key}`);
    config[key] = [...base, ...strings(part[key], `${name}: ${key}`)];
  }
}

delete config.$schema;
const cache = join(root, 'node_modules', '.cache', 'knip');
mkdirSync(cache, { recursive: true });
const merged = join(cache, 'knip.merged.json');
writeFileSync(merged, `${JSON.stringify(config, null, 2)}\n`);

const knip = join(root, 'node_modules', 'knip', 'bin', 'knip.js');
const result = spawnSync(
  process.execPath,
  [knip, '--directory', root, '--config', merged, ...process.argv.slice(2)],
  { stdio: 'inherit' },
);
process.exit(result.status ?? 1);

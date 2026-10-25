#!/usr/bin/env node
/**
 * `npm run check`: ejecuta en orden alfabético cada `scripts/checks/*.mjs` (salvo este) y falla si
 * alguno falla. Cada tarea añade su propia comprobación como archivo nuevo; ninguna edita este.
 * Sin comprobaciones, pasa. Acepta otra carpeta como primer argumento (lo usan sus pruebas).
 *
 * Omite `size.mjs` (VIG-294): mide `dist/` y corre después de `npm run build` (`npm run size` y el
 * trabajo `frontend` de `ci.yml`), no sobre las fuentes.
 */
import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const self = fileURLToPath(import.meta.url);
const directory = resolve(process.argv[2] ?? dirname(self));
/** Comprobaciones que necesitan la construcción y corren después de `npm run build`. */
const AFTER_BUILD = new Set(['size.mjs']);

const checks = readdirSync(directory)
  .filter((name) => name.endsWith('.mjs') && name !== basename(self) && !AFTER_BUILD.has(name))
  .sort();

const failed = [];
for (const name of checks) {
  process.stdout.write(`== comprobación ${name}\n`);
  const result = spawnSync(process.execPath, [join(directory, name)], { stdio: 'inherit' });
  if (result.status !== 0) failed.push(name);
}

if (failed.length > 0) {
  process.stderr.write(
    `Comprobaciones fallidas (${String(failed.length)}): ${failed.join(', ')}\n`,
  );
  process.exit(1);
}
process.stdout.write(`Comprobaciones superadas: ${String(checks.length)}.\n`);

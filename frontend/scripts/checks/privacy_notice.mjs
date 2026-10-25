#!/usr/bin/env node
/**
 * Comprobación de `npm run check` (VIG-321, NFR-APP-33): la copia del aviso de tratamiento que
 * incorpora la aplicación (`src/pages/scr01/privacyNotices.json`, versión → texto) es idéntica a
 * `backend/resources/privacy-notice/<versión>.md`, el texto que sirve U-02: las mismas versiones y
 * el mismo texto byte a byte. Vite no deja importar el `.md` con `?raw` fuera de `frontend/`.
 *
 * Con `--write` regenera la copia desde el backend.
 */
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
const source = join(root, '..', 'backend', 'resources', 'privacy-notice');
const copy = join(root, 'src', 'pages', 'scr01', 'privacyNotices.json');

/** @type {Record<string, string>} */
const expected = {};
for (const name of readdirSync(source)
  .filter((file) => file.endsWith('.md'))
  .sort()) {
  expected[name.slice(0, -'.md'.length)] = readFileSync(join(source, name), 'utf8');
}
const rendered = `${JSON.stringify(expected, null, 2)}\n`;

if (process.argv.includes('--write')) {
  writeFileSync(copy, rendered);
  process.stdout.write(`Aviso de tratamiento copiado: ${Object.keys(expected).join(', ')}.\n`);
  process.exit(0);
}

if (readFileSync(copy, 'utf8') !== rendered) {
  process.stderr.write(
    'src/pages/scr01/privacyNotices.json no coincide con backend/resources/privacy-notice; ' +
      'regenéralo con `node scripts/checks/privacy_notice.mjs --write`.\n',
  );
  process.exit(1);
}
process.stdout.write(`Aviso de tratamiento: ${Object.keys(expected).join(', ')} igual que U-02.\n`);

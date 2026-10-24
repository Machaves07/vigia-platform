#!/usr/bin/env node
/** BR-APP-26/27/29/38/41/83 y D-9. Admite otra carpeta para pruebas de mutación. */
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkTexts } from '../lib/texts_catalog.mjs';

const directory = resolve(
  process.argv[2] ?? fileURLToPath(new URL('../../src/texts', import.meta.url)),
);
const errors = checkTexts(directory);
if (errors.length > 0) {
  process.stderr.write(`${errors.join('\n')}\n`);
  process.exit(1);
}
process.stdout.write('Catálogo de textos: lista propia U-05 y excepción D-9 comprobadas.\n');

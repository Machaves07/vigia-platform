#!/usr/bin/env node
/** NFR-APP-52. Argumento opcional: copia de backend/resources para las sondas. */
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnumerations, loadLabels, missingLabels } from '../lib/labels_catalog.mjs';

const root = fileURLToPath(new URL('../..', import.meta.url));
const resources = resolve(process.argv[2] ?? resolve(root, '../backend/resources'));
const enumerations = loadEnumerations(root);
const errors = missingLabels(enumerations, loadLabels(root, resources));
if (errors.length > 0) {
  process.stderr.write(`${errors.join('\n')}\n`);
  process.exit(1);
}
process.stdout.write(
  `Etiquetas: ${String(enumerations.length)} enumeraciones comprobadas por valor.\n`,
);

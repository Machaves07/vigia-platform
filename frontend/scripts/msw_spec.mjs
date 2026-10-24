#!/usr/bin/env node
/**
 * `node scripts/msw_spec.mjs [--check]`: regenera (o comprueba) `tests/msw/openapi.responses.json`,
 * la copia reducida de `backend/openapi/app.yaml` que usan los manejadores de MSW (VIG-286).
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs';

import { OUTPUT_PATH, renderMswSpec } from './lib/msw_spec.mjs';

const args = process.argv.slice(2);
if (args.some((arg) => arg !== '--check')) {
  process.stderr.write('Uso: node scripts/msw_spec.mjs [--check]\n');
  process.exit(2);
}

const expected = renderMswSpec();
if (args.includes('--check')) {
  const current = existsSync(OUTPUT_PATH) ? readFileSync(OUTPUT_PATH, 'utf8') : null;
  if (current !== expected) {
    process.stderr.write(
      'tests/msw/openapi.responses.json no coincide con backend/openapi/app.yaml: ' +
        'regenérala con `node scripts/msw_spec.mjs`.\n',
    );
    process.exit(1);
  }
  process.stdout.write('tests/msw/openapi.responses.json coincide con app.yaml.\n');
} else {
  writeFileSync(OUTPUT_PATH, expected);
  process.stdout.write('tests/msw/openapi.responses.json regenerada.\n');
}

#!/usr/bin/env node
/**
 * Comprobación de `npm run check` (VIG-286): la copia reducida de la especificación que usan los
 * manejadores de MSW coincide con `backend/openapi/app.yaml`.
 */
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(new URL('../msw_spec.mjs', import.meta.url));
const result = spawnSync(process.execPath, [script, '--check'], { stdio: 'inherit' });
process.exit(result.status ?? 1);

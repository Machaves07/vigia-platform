// @vitest-environment node
/**
 * Operaciones `assumed` (BR-APP-63, VIG-280): las de los puertos de `frontend-components.md` §3
 * que `app.yaml` no tiene. La prueba las calcula por su cuenta desde `app.yaml`, las contrasta con
 * `src/client/assumed.ts`, imprime el informe y espera cero en `main`.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/* eslint-disable no-restricted-imports -- la prueba contrasta el propio cliente generado. */
import {
  ASSUMED_OPERATIONS,
  DESIGN_OPERATIONS,
  isAssumed,
  normalizeOperation,
} from '@/client/assumed';
import { createApiClient, OPERATIONS } from '@/client/index';
/* eslint-enable no-restricted-imports */

import { parseOpenApiYaml } from '../../../scripts/generate/openapiYaml.mjs';
import { listOperations, SPEC_PATH } from '../../../scripts/generate/render.mjs';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const document = parseOpenApiYaml(readFileSync(join(root, SPEC_PATH), 'utf8'));
const specified = new Set(
  listOperations(document).map(
    ({ method, path }) => `${method.toUpperCase()} ${path.replace(/\{[^}]*\}/g, '{}')}`,
  ),
);

describe('operaciones assumed', () => {
  it('contrasta los puertos de frontend-components.md §3 con app.yaml (informe; se esperan cero)', () => {
    const missing = DESIGN_OPERATIONS.filter(
      ({ operation }) => !specified.has(normalizeOperation(operation)),
    );
    process.stdout.write(
      `[assumed] ${String(DESIGN_OPERATIONS.length)} operaciones del diseño, ` +
        `${String(new Set(DESIGN_OPERATIONS.map(({ operation }) => operation)).size)} distintas; ` +
        `faltan en app.yaml: ${String(missing.length)}` +
        missing.map(({ screen, operation }) => `\n  ${screen} ${operation}`).join('') +
        '\n',
    );
    expect(ASSUMED_OPERATIONS).toEqual(missing);
    expect(missing).toEqual([]);
  });

  it('isAssumed: lo que no está en app.yaml es assumed, figure o no en el diseño', () => {
    expect(isAssumed('POST /auth/login')).toBe(false);
    expect(isAssumed('GET /zones/{zone_id}/catalog')).toBe(false);
    expect(isAssumed('GET /zones/{id}/catalog')).toBe(false);
    expect(isAssumed('GET /zones/{id}/catalog/versions/{a}/diff/{b}')).toBe(true);
    expect(isAssumed('DELETE /auth/login')).toBe(true);
    expect(isAssumed('POST /auth/login/')).toBe(true);
  });

  it('normaliza el método y los nombres de los parámetros, no la ruta', () => {
    expect(normalizeOperation('post /zones/{zone_id}/gates/{gate}/revocation')).toBe(
      'POST /zones/{}/gates/{}/revocation',
    );
  });
});

describe('cliente generado', () => {
  it('OPERATIONS tiene cada operación de app.yaml con su método y su ruta', () => {
    expect(
      Object.entries(OPERATIONS).map(([operationId, { method, path }]) => ({
        operationId,
        method,
        path,
      })),
    ).toEqual(
      listOperations(document).map(({ operationId, method, path }) => ({
        operationId,
        method,
        path,
      })),
    );
  });

  it('createApiClient devuelve un cliente sin estado con un método por verbo', () => {
    const client = createApiClient({ baseUrl: 'https://vigia.invalid' });
    for (const verb of ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as const) {
      expect(typeof client[verb]).toBe('function');
    }
    expect(createApiClient()).not.toBe(createApiClient());
  });
});

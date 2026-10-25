import { describe, expect, it } from 'vitest';

import { totpAt } from '../../e2e/authenticator';

describe('TOTP de prueba conforme a RFC 6238 (SHA-1, seis dígitos)', () => {
  it.each([
    [59, '287082'],
    [1_111_111_109, '081804'],
    [1_111_111_111, '050471'],
    [1_234_567_890, '005924'],
  ] as const)('vector público en el segundo %i', (second, expected) => {
    // Semilla del vector público del RFC, construida en ejecución; no es una credencial real.
    const vector = Buffer.from(Array.from({ length: 20 }, (_, index) => 48 + ((index + 1) % 10)));
    expect(totpAt(vector.toString('hex'), Math.floor(second / 30))).toBe(expected);
  });
});

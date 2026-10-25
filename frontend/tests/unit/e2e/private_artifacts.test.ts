import { describe, expect, it } from 'vitest';

import { assertPrivateArtifacts } from '../../e2e/private-artifacts';

describe('el acceso por interfaz no permite grabar material de autenticación', () => {
  it('acepta únicamente los tres mecanismos desactivados', () => {
    expect(() => {
      assertPrivateArtifacts({ trace: 'off', video: 'off', screenshot: 'off' });
    }).not.toThrow();
  });

  it.each([
    ['trazas', { trace: 'retain-on-failure', video: 'off', screenshot: 'off' }],
    ['vídeo', { trace: 'off', video: 'retain-on-failure', screenshot: 'off' }],
    ['capturas', { trace: 'off', video: 'off', screenshot: 'only-on-failure' }],
  ] as const)('rechaza grabación de %s al fallar', (_kind, options) => {
    expect(() => {
      assertPrivateArtifacts(options);
    }).toThrow(/autenticación/);
  });
});

/**
 * PR-APP-26 (LC-APP-01, NFR-APP-31; PAT-APP-REN-01 y RES-07): para **todo** subconjunto de las
 * cinco capacidades del navegador, React se carga si y solo si están todas; si falta alguna, la
 * sección estática «navegador no compatible» de `index.html` queda visible y no se hace ninguna
 * petición.
 *
 * La propiedad recorre los 32 subconjuntos con una máscara de bits, sobre la página real de
 * `index.html` y con la sonda inyectada: el mismo camino que ejecuta el guion en el navegador.
 */
import fc from 'fast-check';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import { bootstrapPage, probeWith } from '../support/capabilities';
import { CAPABILITIES, start, type CapabilityId } from '@/bootstrap/check';

const SUBSETS = 1 << CAPABILITIES.length;

function subsetOf(mask: number): readonly CapabilityId[] {
  return CAPABILITIES.filter((_, index) => (mask & (1 << index)) !== 0);
}

describe('PR-APP-26 · capacidades del navegador', () => {
  let request: MockInstance;

  beforeEach(() => {
    request = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    request.mockRestore();
  });

  it('React se carga si y solo si están todas las capacidades', () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: SUBSETS - 1 }), (mask) => {
        const present = subsetOf(mask);
        const complete = present.length === CAPABILITIES.length;
        const { document, section } = bootstrapPage();
        const loadApp = vi.fn(() => Promise.resolve(undefined));

        const pending = start({ probe: probeWith(present), document, loadApp });

        // La decisión es síncrona: en el mismo turno ya se sabe si el paquete inicial se carga.
        expect(
          loadApp,
          `capacidades presentes: ${present.join(', ') || 'ninguna'}`,
        ).toHaveBeenCalledTimes(complete ? 1 : 0);
        expect(pending === null).toBe(!complete);
        // La sección estática se muestra si y solo si falta alguna capacidad.
        expect(section.hasAttribute('hidden')).toBe(complete);
        // Ninguna petición: el arranque no pide nada, y sin capacidades React no llega a pedirlo.
        expect(request).not.toHaveBeenCalled();
      }),
    );
  });

  it('cubre los dos extremos y las cinco capacidades por separado', () => {
    for (const mask of [0, SUBSETS - 1, ...CAPABILITIES.map((_, index) => 1 << index)]) {
      const present = subsetOf(mask);
      const { document, section } = bootstrapPage();
      const loadApp = vi.fn(() => Promise.resolve(undefined));
      const pending = start({ probe: probeWith(present), document, loadApp });
      const complete = present.length === CAPABILITIES.length;
      expect(loadApp, `máscara ${String(mask)}`).toHaveBeenCalledTimes(complete ? 1 : 0);
      expect(pending === null).toBe(!complete);
      expect(section.hasAttribute('hidden')).toBe(complete);
    }
  });
});

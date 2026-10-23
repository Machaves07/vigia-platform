/**
 * Re-exportaciones de `@vigia/contracts` (LC-APP-22).
 *
 * Los hallazgos de `tests/fixtures/synthetic/contracts/findings.json` los generó el kit de
 * conformidad del contrato (v1.1.0, el mismo commit que el paquete npm) con
 * `generate_findings.py` en esa carpeta.
 */
import { describe, expect, it } from 'vitest';

import {
  contractIssue,
  isAccepted,
  isCompatible,
  labelEs,
  labels,
  validateFinding,
  type Finding,
} from '@/contracts';

import findings from '../fixtures/synthetic/contracts/findings.json';

describe('@/contracts', () => {
  it('valida con el validador re-exportado cada Finding generado por el kit del contrato', () => {
    expect(findings.length).toBeGreaterThan(0);
    for (const finding of findings as unknown[]) {
      expect(validateFinding(finding), JSON.stringify(validateFinding.errors)).toBe(true);
    }
  });

  it('rechaza un Finding alterado con el código del contrato', () => {
    const [first] = findings as unknown as Finding[];
    if (first === undefined) throw new Error('Sin hallazgos de ejemplo.');
    const missing: Record<string, unknown> = { ...first };
    delete missing.zone_id;
    expect(validateFinding(missing)).toBe(false);
    expect(contractIssue(validateFinding)).toEqual({ code: 'schema_invalid', field: 'zone_id' });
    expect(validateFinding({ ...first, extra: true })).toBe(false);
    expect(validateFinding({ ...first, anonymized: false })).toBe(false);
  });

  it('re-exporta las etiquetas de labels.es.json', () => {
    expect(labels.acceptance_status.accepted).toBe('aceptado');
    expect(labelEs('acceptance_status', 'accepted')).toBe('aceptado');
  });

  it('re-exporta isCompatible con la política del contrato', () => {
    const policy = { today: '2026-10-23T00:00:00.000Z' };
    expect(isCompatible('1.1.0', '1.1.0', policy)).toBe('accepted');
    expect(isCompatible('1.2.0', '1.1.0', policy)).toBe('rejected_newer');
    expect(isAccepted(isCompatible('1.0.0', '1.1.0', policy))).toBe(true);
  });
});

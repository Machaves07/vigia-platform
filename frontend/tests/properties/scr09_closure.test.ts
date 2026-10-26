/** PR-APP-07 y 10 de RV-13, aislables también con el perfil nightly de la unidad. */
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';
import { ClosureHistory } from '@/pages/scr09/loop/closure/History';
import type { LedgerRecord } from '@/ports/scr09Closure';
import {
  assertNoPersonData,
  assertOrganizationIsolation,
  organizationLeaks,
} from '../support/viewProperties';
import registrations from '../views/scr09_closure';
import type { ViewRegistration } from '../views/types';

const OWN = 'c8e2df00-b213-4687-9aab-8c42e1504204';
const FOREIGN = '9f10a928-000b-4000-859b-b3c529534544';

/**
 * Contraejemplo de la semilla 1457963427, reducido a mano: la reapertura propia termina en
 * «"DpropertyIsEnumerabl» y el rótulo siguiente empieza por «Escalamiento»; unidos y plegados
 * contienen la `description` de la evidencia ajena, que la vista no pinta.
 */
const record = (
  index: number,
  organization: string,
  recordType: string,
  content: Record<string, unknown>,
) => ({
  record_id: `0190a1b2-0357-7000-8000-00000000000${String(index)}`,
  organization_id: organization,
  record_type: recordType,
  received_at: '2026-10-24T15:04:05.123Z',
  content,
});
const coincidence = {
  items: [
    record(1, OWN, 'reopening', { reason_text: '"DpropertyIsEnumerabl' }),
    record(2, FOREIGN, 'action_evidence_added', { description: ':propertyIsEnumerable' }),
    record(3, OWN, 'action_overdue', { escalation_level: 'coordinator' }),
  ],
};
const session = { organizationId: OWN, concessionOrganizationId: null };

describe('RV-13 de cierre', { timeout: 120_000 }, () => {
  it('solo pinta los campos declarados del historial', () => {
    assertNoPersonData(registrations.history);
  });
  it('solo pinta los campos declarados de vencidos', () => {
    assertNoPersonData(registrations.overdue);
  });
  it('no pinta registros de otra organización y registra el error de contrato', () => {
    assertOrganizationIsolation(registrations.history);
  });
  it('regresión de la semilla 1457963427: un texto propio unido a un rótulo no es fuga', () => {
    expect(organizationLeaks(registrations.history, coincidence, session, [['items', 1]])).toEqual(
      [],
    );
  });
  it('regresión de la semilla 1457963427: cae en rojo si la vista pinta el registro ajeno', () => {
    const leaky: ViewRegistration = {
      ...registrations.history,
      render: (data, context) =>
        createElement(ClosureHistory, {
          records: (data.items as LedgerRecord[]).map((item) => ({
            ...item,
            organization_id: context.organizationId,
          })),
          timeZone: 'America/Bogota',
          organizationId: context.organizationId,
          reportContractError: context.reportContractError,
        }),
    };
    expect(organizationLeaks(leaky, coincidence, session, [['items', 1]])).toContain(
      'RV-13 (SCR-09): «items[1].content.description» de un registro de otra organización aparece en el texto.',
    );
  });
});

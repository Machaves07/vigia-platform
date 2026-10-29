/**
 * VIG-363 (hallazgo 20 de VIG-338): el lazo ofrece solo los registros que admite el estado que da
 * el servidor. `LOOP_ACCESS` no es una regla propia (BR-APP-43): se contrasta por igualdad con la
 * tabla de transiciones que aplica el servidor (`lifecycle` de `backend/openapi/u05-fixtures.json`).
 * Una prueba de componente por estado; las aserciones comparan atributos, claves del catálogo y
 * textos exactos, nunca subcadenas con datos (VIG-194).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { LoopView } from '@/pages/scr09/loop/LoopView';
import {
  LOOP_ACCESS,
  loopAccess,
  unclosableCommitment,
  type LoopAccess,
} from '@/pages/scr09/loop/loopAccess';
import { text } from '@/texts';
import { detail, INSTANT } from './classify_support';

interface Transition {
  readonly id: string;
  readonly from: readonly string[];
  readonly to: string;
  readonly permissions: readonly string[];
  readonly records: readonly string[];
}

const lifecycle = (
  JSON.parse(
    readFileSync(
      resolve(
        dirname(fileURLToPath(import.meta.url)),
        '../../../../backend/openapi/u05-fixtures.json',
      ),
      'utf8',
    ),
  ) as { lifecycle: { states: string[]; transitions: Transition[] } }
).lifecycle;

function origins(predicate: (transition: Transition) => boolean): Set<string> {
  return new Set(lifecycle.transitions.filter(predicate).flatMap((t) => t.from));
}

function statesWith(access: LoopAccess): Set<string> {
  return new Set(
    Object.entries(LOOP_ACCESS)
      .filter(([, offered]) => offered.includes(access))
      .map(([state]) => state),
  );
}

const DUE = '2026-12-08T17:00:00.000Z';
const RENEWED = '2027-03-01T17:00:00.000Z';
const MEASURE = 'Delimitar el acceso con una barrera física temporal en la zona';
const DECLARATION = '0190a1b2-9999-7000-8000-000000000363';

afterEach(() => {
  cleanup();
});

describe('registros del lazo por estado contra la tabla del servidor (BR-APP-43)', () => {
  it('cubre exactamente los estados de finding_lifecycle_state', () => {
    expect(Object.keys(LOOP_ACCESS).sort()).toEqual([...lifecycle.states].sort());
  });

  it('comprometer la acción: los orígenes de action_assigned sin reapertura ni revisión (T07)', () => {
    expect(statesWith('action')).toEqual(
      origins(
        (t) =>
          t.records.includes('action_assigned') &&
          !t.records.includes('reopening') &&
          !t.records.includes('unclosable_review'),
      ),
    );
  });

  it.each([
    ['closure', 'closure'],
    ['unclosable', 'unclosable_state_declared'],
    ['unclosable_review', 'unclosable_review'],
    ['reopening', 'reopening'],
  ] as const)('%s: los estados desde donde sale el registro %s', (access, record) => {
    expect(statesWith(access)).toEqual(origins((t) => t.records.includes(record)));
  });

  it('la clasificación no es un registro del lazo: ningún acceso la escribe', () => {
    const loopRecords = new Set([
      'action_assigned',
      'closure',
      'unclosable_state_declared',
      'unclosable_review',
      'reopening',
    ]);
    const withLoopRecord = origins((t) => t.records.some((record) => loopRecords.has(record)));
    const offered = new Set(
      Object.entries(LOOP_ACCESS)
        .filter(([, list]) => list.length > 0)
        .map(([state]) => state),
    );
    expect(offered).toEqual(withLoopRecord);
  });

  it('un estado fuera de la lista no ofrece nada', () => {
    expect(loopAccess('zone_cleared')).toEqual([]);
    expect(loopAccess('constructor')).toEqual([]);
  });
});

function record(
  type: string,
  sequence: number,
  content: Record<string, unknown>,
): { record_type: string; chain_sequence: number; content: unknown } {
  return { record_type: type, chain_sequence: sequence, content };
}

describe('la revisión comprometida sale de la última declaración (unclosableCommitment)', () => {
  const declared = record('unclosable_state_declared', 10, {
    declaration_id: DECLARATION,
    review_due_at: DUE,
    compensatory_measure: MEASURE,
  });

  it('sin declaración no hay compromiso', () => {
    expect(unclosableCommitment([])).toBeNull();
    expect(unclosableCommitment([record('closure', 1, { closure_id: DECLARATION })])).toBeNull();
  });

  it('la declaración da su fecha y su medida', () => {
    expect(unclosableCommitment([declared])).toEqual({
      reviewDueAt: DUE,
      compensatoryMeasure: MEASURE,
    });
  });

  it('una renovación posterior de esa declaración cambia la fecha, en el orden de la cadena', () => {
    const renewed = record('unclosable_review', 11, {
      declaration_id: DECLARATION,
      outcome: 'renewed',
      new_review_due_at: RENEWED,
    });
    expect(unclosableCommitment([renewed, declared])).toEqual({
      reviewDueAt: RENEWED,
      compensatoryMeasure: MEASURE,
    });
  });

  it('ni la conversión en acción ni la revisión de otra declaración cambian la fecha', () => {
    const converted = record('unclosable_review', 11, {
      declaration_id: DECLARATION,
      outcome: 'converted_to_action',
      new_review_due_at: RENEWED,
    });
    const foreign = record('unclosable_review', 12, {
      declaration_id: '0190a1b2-9999-7000-8000-000000000999',
      outcome: 'renewed',
      new_review_due_at: RENEWED,
    });
    expect(unclosableCommitment([declared, converted, foreign])?.reviewDueAt).toBe(DUE);
  });

  it('una declaración nueva reemplaza a la anterior', () => {
    const later = record('unclosable_state_declared', 20, {
      declaration_id: '0190a1b2-9999-7000-8000-000000000020',
      review_due_at: RENEWED,
      compensatory_measure: 'Otra medida compensatoria sintética de la zona',
    });
    expect(unclosableCommitment([later, declared])).toEqual({
      reviewDueAt: RENEWED,
      compensatoryMeasure: 'Otra medida compensatoria sintética de la zona',
    });
  });
});

describe('estado de la acción tras el cierre firmado (VIG-363, diagnóstico en el PR)', () => {
  function action(state: string) {
    return {
      action_id: '0190a1b2-7777-7000-8000-000000000363',
      record_id: '0190a1b2-7777-7000-8000-000000000364',
      action_type: 'engineering',
      action_description: 'Instalar una barrera fija en el acceso lateral de la prensa',
      expected_outcome: null,
      state,
      assigned_at: INSTANT,
      due_at: DUE,
      counts_as_overdue: true,
      root_cause_analysis_id: null,
    };
  }

  function shown(lifecycle_state: string, state: string) {
    const { container } = render(
      <LoopView
        detail={detail({ lifecycle_state, actions: [action(state)] })}
        zone={null}
        timeZone="UTC"
      />,
    );
    const node = container.querySelector('[data-part="action_state"]');
    const value = [node?.getAttribute('data-value'), node?.textContent];
    cleanup();
    return value;
  }

  it.each(['assigned', 'overdue'])(
    'con el hallazgo cerrado, la acción vigente (%s) se presenta cerrada con el hallazgo',
    (state) => {
      expect(shown('closed', state)).toEqual(['closed', text('scr09.loop.action_state.closed')]);
    },
  );

  it('una acción sustituida sigue sustituida, y con el lazo abierto se muestra la del servidor', () => {
    expect(shown('closed', 'superseded')).toEqual([
      'superseded',
      text('scr09.loop.action_state.superseded'),
    ]);
    expect(shown('action_assigned', 'assigned')).toEqual([
      'assigned',
      text('scr09.loop.action_state.assigned'),
    ]);
    expect(shown('unclosable', 'assigned')).toEqual([
      'assigned',
      text('scr09.loop.action_state.assigned'),
    ]);
  });
});

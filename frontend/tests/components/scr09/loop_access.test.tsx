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
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { LoopNext } from '@/pages/scr09/loop/LoopNext';
import { LoopView } from '@/pages/scr09/loop/LoopView';
import {
  LOOP_ACCESS,
  LOOP_PRIMARY,
  loopAccess,
  unclosableCommitment,
  type LoopAccess,
} from '@/pages/scr09/loop/loopAccess';
import { text } from '@/texts';
import { exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { detail, FINDING, INSTANT, renderScreen, WAIT } from './classify_support';

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

const ALL = new Set([
  'findings.read',
  'findings.classify',
  'actions.manage',
  'closure_attachments.read',
  'findings.close_and_sign',
]);
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

  it('el destacado es uno de los registros que el estado admite', () => {
    for (const [state, primary] of Object.entries(LOOP_PRIMARY)) {
      expect(loopAccess(state)).toContain(primary);
    }
    expect(LOOP_PRIMARY).toEqual({ unclosable: 'unclosable_review' });
  });

  it('un estado fuera de la lista no ofrece nada', () => {
    expect(loopAccess('zone_cleared')).toEqual([]);
    expect(loopAccess('constructor')).toEqual([]);
  });
});

function renderNext(
  state: string,
  permissions: ReadonlySet<string> = ALL,
  commitment: Parameters<typeof LoopNext>[0]['commitment'] = null,
) {
  return render(
    <MemoryRouter>
      <LoopNext
        findingId={FINDING}
        state={state}
        permissions={permissions}
        commitment={commitment}
        timeZone="America/Bogota"
      />
    </MemoryRouter>,
  );
}

function offered(container: HTMLElement) {
  return [...container.querySelectorAll('li[data-access]')].map((item) => [
    item.getAttribute('data-access'),
    item.querySelector('a')?.getAttribute('href'),
    item.getAttribute('data-primary'),
  ]);
}

describe('«Siguientes registros del lazo», un estado cada vez', () => {
  it('cerrado: solo la reapertura', () => {
    const { container } = renderNext('closed');
    expect(offered(container)).toEqual([
      ['reopening', `/findings/${FINDING}/loop/reopening`, null],
    ]);
  });

  it('incerrable: la revisión destacada con fecha y medida, y la reapertura', async () => {
    const { container } = renderNext('unclosable', ALL, {
      reviewDueAt: DUE,
      compensatoryMeasure: MEASURE,
    });
    expect(offered(container)).toEqual([
      ['unclosable_review', `/findings/${FINDING}/loop/unclosable`, 'true'],
      ['reopening', `/findings/${FINDING}/loop/reopening`, null],
    ]);
    const primary = container.querySelector('li[data-primary="true"]');
    expect(primary?.querySelector('a')?.textContent).toBe(
      text('role_navigation.loop.unclosable_review'),
    );
    const facts = [...(primary?.querySelectorAll('dt, dd') ?? [])].map((node) => node.textContent);
    expect(facts[0]).toBe(text('role_navigation.loop.review_due_at'));
    expect(primary?.querySelector('dd time')?.getAttribute('datetime')).toBe(DUE);
    expect(facts.slice(2)).toEqual([text('scr09.unclosable.compensatory_measure'), MEASURE]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('incerrable sin la declaración leída: la revisión sigue destacada y lo dice', () => {
    const { container } = renderNext('unclosable');
    const primary = container.querySelector('li[data-primary="true"]');
    expect(primary?.getAttribute('data-access')).toBe('unclosable_review');
    expect(primary?.querySelector('dl')).toBeNull();
    expect(
      [...(primary?.querySelectorAll('p') ?? [])].map((paragraph) => paragraph.textContent),
    ).toEqual([
      text('role_navigation.loop.unclosable_review_hint'),
      text('role_navigation.loop.commitment_unknown'),
    ]);
  });

  it('abierto: declarar incerrable (la acción se compromete con el formulario de la pantalla)', () => {
    const { container } = renderNext('open');
    expect(offered(container)).toEqual([
      ['unclosable', `/findings/${FINDING}/loop/unclosable`, null],
    ]);
  });

  it.each(['action_assigned', 'overdue'])('%s: evidencia y cierre, e incerrable', (state) => {
    const { container } = renderNext(state);
    expect(offered(container)).toEqual([
      ['closure', `/findings/${FINDING}/loop/closure`, null],
      ['unclosable', `/findings/${FINDING}/loop/unclosable`, null],
    ]);
  });

  it.each(['received', 'in_review', 'discarded', 'suspended_by_reclassification'])(
    '%s: ningún registro del lazo, con la explicación',
    (state) => {
      const { container } = renderNext(state);
      expect(offered(container)).toEqual([]);
      const nav = screen.getByRole('navigation', { name: text('scr09.loop.next') });
      expect(nav.querySelector('[data-part="loop_next_none"]')?.textContent).toBe(
        text('role_navigation.loop.none'),
      );
    },
  );

  it('solo con actions.manage: la reapertura sí, la revisión y el cierre firmado no', () => {
    const manage = new Set(['findings.read', 'actions.manage']);
    expect(offered(renderNext('unclosable', manage).container)).toEqual([
      ['reopening', `/findings/${FINDING}/loop/reopening`, null],
    ]);
    cleanup();
    expect(offered(renderNext('closed', manage).container)).toEqual([
      ['reopening', `/findings/${FINDING}/loop/reopening`, null],
    ]);
  });

  it('sin claves de gestión, en un estado con registros, no hay navegación', () => {
    renderNext('closed', new Set(['findings.read']));
    expect(screen.queryByRole('navigation', { name: text('scr09.loop.next') })).toBeNull();
  });

  it('la revisión pide su pestaña por el estado de la navegación, sin consulta en la URL', async () => {
    function Probe() {
      const location = useLocation();
      return (
        <p data-part="probe">
          {JSON.stringify([location.pathname, location.search, location.state])}
        </p>
      );
    }
    const { container } = render(
      <MemoryRouter initialEntries={['/lazo']}>
        <Routes>
          <Route
            path="/lazo"
            element={
              <LoopNext
                findingId={FINDING}
                state="unclosable"
                permissions={ALL}
                commitment={null}
                timeZone="UTC"
              />
            }
          />
          <Route path="/findings/:id/loop/unclosable" element={<Probe />} />
          <Route path="/findings/:id/loop/reopening" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    const link = container.querySelector('li[data-primary="true"] a');
    if (link === null) throw new Error('Falta la revisión destacada');
    await userEvent.setup().click(link);
    expect(screen.getByText(/loop/u, { selector: '[data-part="probe"]' }).textContent).toBe(
      JSON.stringify([`/findings/${FINDING}/loop/unclosable`, '', { review: true }]),
    );
  });

  it('la reapertura no pide la pestaña de revisión', async () => {
    function Probe() {
      const location = useLocation();
      return <p data-part="probe">{JSON.stringify(location.state)}</p>;
    }
    const { container } = render(
      <MemoryRouter initialEntries={['/lazo']}>
        <Routes>
          <Route
            path="/lazo"
            element={
              <LoopNext
                findingId={FINDING}
                state="unclosable"
                permissions={ALL}
                commitment={null}
                timeZone="UTC"
              />
            }
          />
          <Route path="/findings/:id/loop/reopening" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    const link = container.querySelector('li[data-access="reopening"] a');
    if (link === null) throw new Error('Falta la reapertura');
    await userEvent.setup().click(link);
    expect(container.querySelector('[data-part="probe"]')?.textContent).toBe('null');
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

describe('la pantalla del lazo según el estado del servidor', { timeout: 40_000 }, () => {
  const PERMISSIONS = [...ALL, 'hierarchy.read'];

  /** Los registros del expediente que sirve el servidor falso, de la organización de la sesión. */
  type Records = (organizationId: string) => unknown[];

  async function load(lifecycle_state: string, records: Records = () => []) {
    let ledgerReads = 0;
    let organization = '';
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}`, () =>
        HttpResponse.json(detail({ lifecycle_state })),
      ),
      http.get('http://localhost/ledger/records', () => {
        ledgerReads++;
        return HttpResponse.json({ items: records(organization), next_cursor: null });
      }),
    );
    const rendered = await renderScreen('loop', { permissions: PERMISSIONS });
    organization = rendered.organizationId;
    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: text('scr09.loop.load') }, WAIT));
    const nav = await screen.findByRole('navigation', { name: text('scr09.loop.next') }, WAIT);
    return { ...rendered, ledgerReads: () => ledgerReads, nav };
  }

  it('cerrado: solo la reapertura y ningún formulario de acción', async () => {
    const { nav, ledgerReads } = await load('closed');
    expect(offered(nav)).toEqual([['reopening', `/findings/${FINDING}/loop/reopening`, null]]);
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
    expect(ledgerReads()).toBe(0);
  });

  it('abierto: el formulario de la acción e incerrable, sin cierre ni reapertura', async () => {
    const { nav } = await load('open');
    expect(offered(nav)).toEqual([['unclosable', `/findings/${FINDING}/loop/unclosable`, null]]);
    await screen.findByRole('form', { name: text('scr09.loop.action_form') }, WAIT);
  });

  it('incerrable: la revisión destacada con la fecha y la medida de su declaración', async () => {
    const page = exampleFor('list_records_ledger_records_get') as {
      items: Record<string, unknown>[];
    };
    const base = page.items[0] ?? {};
    const findingDetail = detail();
    const { nav, ledgerReads } = await load('unclosable', (organization_id) => [
      {
        ...base,
        organization_id,
        record_type: 'unclosable_state_declared',
        chain_sequence: 7,
        plant_id: findingDetail.plant_id,
        scope: { plant_id: findingDetail.plant_id, zone_id: findingDetail.zone_id, node_id: null },
        content: {
          subject_id: findingDetail.subject_id,
          declaration_id: DECLARATION,
          review_due_at: DUE,
          compensatory_measure: MEASURE,
        },
      },
    ]);
    await waitFor(() => {
      expect(nav.querySelector('li[data-primary="true"] dl')).not.toBeNull();
    }, WAIT);
    expect(within(nav).getByText(MEASURE).tagName).toBe('DD');
    expect(ledgerReads()).toBe(1);
    expect(screen.queryByRole('form', { name: text('scr09.loop.action_form') })).toBeNull();
  });
});

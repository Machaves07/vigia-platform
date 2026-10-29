/**
 * VIG-365 (S3-13 y S3-24): el panel «Siguiente paso» da una sola acción principal por estado del
 * servidor. `NEXT_STEP` no es una regla propia (BR-APP-43): cada acceso se contrasta por igualdad
 * con los orígenes de su transición en la tabla de U-04 (`lifecycle` de
 * `backend/openapi/u05-fixtures.json`), menos las omisiones que decidió el dueño, que se nombran
 * aquí una a una. Una prueba de componente por estado; las aserciones comparan atributos, claves
 * del catálogo y textos exactos, nunca subcadenas con datos (VIG-194).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { NEXT_STEP, nextStep, type StepAccess } from '@/pages/shared/nextStep';
import { NextStepPanel } from '@/pages/shared/NextStepPanel';
import { text } from '@/texts';

interface Transition {
  readonly id: string;
  readonly from: readonly string[];
  readonly records: readonly string[];
}

const lifecycle = (
  JSON.parse(
    readFileSync(
      resolve(
        dirname(fileURLToPath(import.meta.url)),
        '../../../backend/openapi/u05-fixtures.json',
      ),
      'utf8',
    ),
  ) as { lifecycle: { states: string[]; transitions: Transition[] } }
).lifecycle;

function origins(predicate: (transition: Transition) => boolean): Set<string> {
  return new Set(lifecycle.transitions.filter(predicate).flatMap((t) => t.from));
}

/** Los estados desde donde sale cada acceso según la tabla del servidor. */
const ORIGINS: Readonly<Record<StepAccess, Set<string>>> = {
  classify: origins((t) => t.records.includes('classification') && t.from.includes('received')),
  review_queue: origins((t) => t.records.includes('review_resolution')),
  action: origins(
    (t) =>
      t.records.includes('action_assigned') &&
      !t.records.includes('reopening') &&
      !t.records.includes('unclosable_review'),
  ),
  closure: origins((t) => t.records.includes('closure')),
  unclosable: origins((t) => t.records.includes('unclosable_state_declared')),
  unclosable_review: origins((t) => t.records.includes('unclosable_review')),
  reopening: origins((t) => t.records.includes('reopening')),
  reclassify: origins(
    (t) => t.records.includes('classification') && !t.from.every((state) => state === 'received'),
  ),
};

/**
 * Omisiones deliberadas (decisión del dueño en VIG-365): en lazo abierto no se ofrece declarar
 * incerrable; con acción, vencida, cerrada o incerrable no se ofrece reclasificar; un incerrable no
 * ofrece la reapertura. Siguen en sus pantallas: el servidor decide al registrar.
 */
const OMITTED: Readonly<Partial<Record<StepAccess, readonly string[]>>> = {
  unclosable: ['open'],
  reopening: ['unclosable'],
  reclassify: ['action_assigned', 'overdue', 'closed', 'unclosable'],
};

function offeredIn(access: StepAccess): Set<string> {
  return new Set(
    Object.entries(NEXT_STEP)
      .filter(([, step]) => step.primary === access || step.secondary.includes(access))
      .map(([state]) => state),
  );
}

const ALL = new Set([
  'findings.read',
  'findings.classify',
  'review_queue.resolve',
  'actions.manage',
  'closure_attachments.read',
  'findings.close_and_sign',
]);
const FINDING = '0190a1b2-2222-7000-8000-000000000365';
const DUE = '2026-12-08T17:00:00.000Z';
const MEASURE = 'Delimitar el acceso con una barrera física temporal en la zona';

afterEach(() => {
  cleanup();
});

describe('siguiente paso por estado contra la tabla del servidor (BR-APP-43)', () => {
  it('cubre exactamente los estados de finding_lifecycle_state', () => {
    expect(Object.keys(NEXT_STEP).sort()).toEqual([...lifecycle.states].sort());
  });

  it.each(Object.keys(ORIGINS) as StepAccess[])(
    '%s: se ofrece en los orígenes de su transición, menos las omisiones nombradas',
    (access) => {
      const expected = new Set(
        [...ORIGINS[access]].filter((state) => !(OMITTED[access] ?? []).includes(state)),
      );
      expect(offeredIn(access)).toEqual(expected);
    },
  );

  it('cada estado tiene a lo sumo una acción principal, y es una que su tabla admite', () => {
    for (const [state, step] of Object.entries(NEXT_STEP)) {
      if (step.primary !== null) expect(ORIGINS[step.primary].has(state)).toBe(true);
      expect(step.secondary).not.toContain(step.primary);
    }
  });

  it('un estado fuera de la lista no ofrece nada', () => {
    expect(nextStep('zone_cleared')).toEqual({ primary: null, secondary: [] });
    expect(nextStep('constructor')).toEqual({ primary: null, secondary: [] });
  });
});

function renderPanel(
  state: string,
  {
    permissions = ALL,
    onAction,
    commitment = null,
  }: {
    permissions?: ReadonlySet<string>;
    onAction?: () => void;
    commitment?: { reviewDueAt: string; compensatoryMeasure: string } | null;
  } = {},
) {
  return render(
    <MemoryRouter>
      <NextStepPanel
        findingId={FINDING}
        state={state}
        permissions={permissions}
        dueAt={DUE}
        commitment={commitment}
        timeZone="America/Bogota"
        {...(onAction === undefined ? {} : { onAction })}
      />
    </MemoryRouter>,
  );
}

/** [acceso, href o `button`, principal] de cada control del panel, en orden. */
function controls(container: HTMLElement) {
  return [...container.querySelectorAll('[data-access]')].map((node) => [
    node.getAttribute('data-access'),
    node.tagName === 'A' ? node.getAttribute('href') : 'button',
    node.getAttribute('data-primary'),
  ]);
}

describe('panel «Siguiente paso», un estado cada vez', () => {
  const path = (tail: string) => `/findings/${FINDING}/${tail}`;

  it.each([
    ['received', [['classify', path('classify'), 'true']]],
    ['in_review', [['review_queue', '/review-queue', 'true']]],
    [
      'open',
      [
        ['action', path('loop'), 'true'],
        ['reclassify', path('classify'), null],
      ],
    ],
    [
      'action_assigned',
      [
        ['closure', path('loop/closure'), 'true'],
        ['unclosable', path('loop/unclosable'), null],
      ],
    ],
    [
      'overdue',
      [
        ['closure', path('loop/closure'), 'true'],
        ['unclosable', path('loop/unclosable'), null],
      ],
    ],
    ['unclosable', [['unclosable_review', path('loop/unclosable'), 'true']]],
    ['closed', [['reopening', path('loop/reopening'), 'true']]],
    ['suspended_by_reclassification', [['reclassify', path('classify'), 'true']]],
    ['discarded', [['reclassify', path('classify'), null]]],
  ] as const)('%s: sus controles exactos', async (state, expected) => {
    const { container } = renderPanel(state);
    expect(controls(container)).toEqual(expected);
    expect(container.querySelectorAll('[data-primary="true"]').length).toBeLessThanOrEqual(1);
    expect(container.querySelector('[data-part="why"]')?.textContent).toBe(
      text(`next_step.why.${state}`),
    );
    const primary = container.querySelector('[data-primary="true"]');
    if (primary !== null) {
      expect(primary.textContent).toBe(
        text(`next_step.access.${primary.getAttribute('data-access') ?? ''}`),
      );
    }
    expect(await axe(container)).toHaveNoViolations();
  });

  it('cerrado: la reapertura es lo único y no hay «Más acciones»', () => {
    const { container } = renderPanel('closed');
    expect(container.querySelector('[data-part="nothing_pending"]')).toBeNull();
    expect(screen.queryByRole('heading', { name: text('next_step.more') })).toBeNull();
  });

  it('descartado: sin acción principal, «No hay nada pendiente» y reclasificar como secundaria', () => {
    const { container } = renderPanel('discarded');
    expect(container.querySelector('[data-part="nothing_pending"]')?.textContent).toBe(
      text('next_step.nothing_pending'),
    );
  });

  it('en el lazo, «Comprometer una acción» es un botón que abre el formulario', async () => {
    let opened = 0;
    const { container } = renderPanel('open', {
      onAction: () => {
        opened += 1;
      },
    });
    expect(controls(container)[0]).toEqual(['action', 'button', 'true']);
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: text('next_step.access.action') }));
    expect(opened).toBe(1);
  });

  it('en lazo abierto, reclasificar conserva su aviso de suspensión (BR-LAZ-24)', () => {
    const { container } = renderPanel('open');
    expect(container.querySelector('[data-part="suspends"]')?.textContent).toBe(
      text('finding_journey.suspends'),
    );
  });

  it('con acción o vencida, el porqué lleva el plazo de la acción', () => {
    const { container } = renderPanel('overdue');
    expect(container.querySelector('[data-part="due"] time')?.getAttribute('datetime')).toBe(DUE);
  });

  it('incerrable: la revisión con su fecha comprometida y su medida compensatoria', () => {
    const { container } = renderPanel('unclosable', {
      commitment: { reviewDueAt: DUE, compensatoryMeasure: MEASURE },
    });
    const facts = container.querySelector('[data-part="unclosable_commitment"]');
    expect(facts?.querySelector('dd time')?.getAttribute('datetime')).toBe(DUE);
    expect([...(facts?.querySelectorAll('dd') ?? [])].at(-1)?.textContent).toBe(MEASURE);
  });

  it('solo con lectura: en un estado con pasos que no puede abrir, el panel no aparece', () => {
    const { container } = renderPanel('closed', { permissions: new Set(['findings.read']) });
    expect(container.querySelector('[data-part="next_step"]')).toBeNull();
  });

  it('cada paso pide lo suyo por el estado de la navegación, nunca por la URL', async () => {
    function Probe() {
      const location = useLocation();
      return <p data-part="probe">{JSON.stringify([location.search, location.state])}</p>;
    }
    const { container } = render(
      <MemoryRouter initialEntries={['/detalle']}>
        <Routes>
          <Route
            path="/detalle"
            element={
              <NextStepPanel
                findingId={FINDING}
                state="unclosable"
                permissions={ALL}
                timeZone="UTC"
              />
            }
          />
          <Route path="/findings/:id/loop/unclosable" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    const link = container.querySelector('a[data-primary="true"]');
    if (link === null) throw new Error('Falta la revisión');
    await userEvent.setup().click(link);
    expect(within(container).getByText(/review/u).textContent).toBe(
      JSON.stringify(['', { review: true }]),
    );
  });
});

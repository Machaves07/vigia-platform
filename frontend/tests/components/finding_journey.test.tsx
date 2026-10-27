/**
 * VIG-359: recorrido diario guiado por el estado del hallazgo. `FINDING_ACCESS` no es una regla
 * propia de la interfaz (BR-APP-43): se contrasta por igualdad con la tabla de transiciones que
 * aplica el servidor (`lifecycle` de `backend/openapi/u05-fixtures.json`). Las aserciones comparan
 * claves del catálogo, rutas y atributos exactos; nunca subcadenas con datos (VIG-194).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { textViolations } from '../../scripts/lib/texts_catalog.mjs';
import {
  FINDING_ACCESS,
  FindingActions,
  RESUMING_STATE,
  SUSPENDING_STATES,
  findingAccess,
  reviewQueuePath,
  type FindingAccess,
} from '@/pages/shared/FindingActions';
import journey from '@/texts/finding_journey.json';
import { text } from '@/texts';

const FINDING = '0190a1b2-2222-7000-8000-000000000359';
const DETECTION = '0190a1b2-4444-7000-8000-000000000359';
const COORDINATOR = new Set(['findings.read', 'findings.classify', 'actions.manage']);

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
        '../../../backend/openapi/u05-fixtures.json',
      ),
      'utf8',
    ),
  ) as { lifecycle: { states: string[]; transitions: Transition[] } }
).lifecycle;

function origins(predicate: (transition: Transition) => boolean): Set<string> {
  return new Set(lifecycle.transitions.filter(predicate).flatMap((t) => t.from));
}

function statesWith(access: FindingAccess): Set<string> {
  return new Set(
    Object.entries(FINDING_ACCESS)
      .filter(([, offered]) => offered.includes(access))
      .map(([state]) => state),
  );
}

afterEach(() => {
  cleanup();
});

describe('acceso por estado contra la tabla del servidor (BR-APP-43)', () => {
  // Los estados en que nace un sujeto (T01, T02): de ahí sale la primera decisión humana.
  const arrival = new Set(
    lifecycle.transitions.filter((t) => t.from.length === 0).map((t) => t.to),
  );

  it('cubre exactamente los estados de finding_lifecycle_state', () => {
    expect(Object.keys(FINDING_ACCESS).sort()).toEqual([...lifecycle.states].sort());
  });

  it('la cola es el único acceso desde donde sale una resolución de la cola (T05, T06)', () => {
    expect(statesWith('review_queue')).toEqual(
      origins((t) => t.records.includes('review_resolution')),
    );
  });

  it('clasificar y reclasificar son los estados desde donde sale una clasificación', () => {
    const classification = origins((t) => t.records.includes('classification'));
    expect(new Set([...statesWith('classify'), ...statesWith('reclassify')])).toEqual(
      classification,
    );
    // La primera clasificación sale del estado de llegada; las demás son reclasificaciones.
    expect(statesWith('classify')).toEqual(
      new Set([...classification].filter((state) => arrival.has(state))),
    );
    expect(statesWith('reclassify')).toEqual(
      new Set([...classification].filter((state) => !arrival.has(state))),
    );
  });

  it('el lazo existe en todo estado posterior a la llegada', () => {
    expect(statesWith('loop')).toEqual(
      new Set(lifecycle.states.filter((state) => !arrival.has(state))),
    );
  });

  it('suspende el lazo la reclasificación desde sus estados de origen (T15, BR-LAZ-24)', () => {
    expect(new Set(SUSPENDING_STATES)).toEqual(
      origins((t) => t.to === 'suspended_by_reclassification'),
    );
    expect(origins((t) => t.to === 'previous')).toEqual(new Set([RESUMING_STATE]));
  });

  it('un estado fuera de la lista cerrada no ofrece ningún acceso', () => {
    expect(findingAccess('estado_futuro')).toEqual([]);
    expect(findingAccess('constructor')).toEqual([]);
    expect(findingAccess('__proto__')).toEqual([]);
  });
});

function renderActions(
  state: string,
  {
    permissions = COORDINATOR,
    detectionId = null,
  }: { permissions?: ReadonlySet<string>; detectionId?: string | null } = {},
) {
  return render(
    <MemoryRouter>
      <FindingActions
        findingId={FINDING}
        state={state}
        detectionId={detectionId}
        permissions={permissions}
        landmark
      />
    </MemoryRouter>,
  );
}

function links() {
  return screen
    .queryAllByRole('link')
    .map((link) => [link.textContent, link.getAttribute('href')] as const);
}

describe('FindingActions', () => {
  it.each([
    {
      state: 'in_review',
      expected: [[text('finding_journey.review_queue'), `/review-queue?detection=${DETECTION}`]],
    },
    {
      state: 'received',
      expected: [[text('finding_journey.classify'), `/findings/${FINDING}/classify`]],
    },
    ...['open', 'action_assigned', 'overdue', 'suspended_by_reclassification'].map((state) => ({
      state,
      expected: [
        [text('finding_journey.loop'), `/findings/${FINDING}/loop`],
        [text('finding_journey.reclassify'), `/findings/${FINDING}/classify`],
      ],
    })),
    ...['closed', 'unclosable', 'discarded'].map((state) => ({
      state,
      expected: [
        [text('finding_journey.loop'), `/findings/${FINDING}/loop`],
        [text('finding_journey.reclassify'), `/findings/${FINDING}/classify`],
      ],
    })),
  ])('$state ofrece sus accesos y solo esos', async ({ state, expected }) => {
    const { container } = renderActions(state, { detectionId: DETECTION });
    expect(links()).toEqual(expected);
    const navigation = screen.getByRole('navigation', { name: text('finding_journey.navigation') });
    expect(navigation).toBeTruthy();
    const suspends = container.querySelector('[data-part="suspends"]');
    expect(suspends?.textContent.trim() ?? null).toBe(
      SUSPENDING_STATES.includes(state) ? text('finding_journey.suspends') : null,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('sin la detección, la cola se abre entera', () => {
    renderActions('in_review');
    expect(links()).toEqual([[text('finding_journey.review_queue'), '/review-queue']]);
    expect(reviewQueuePath(null)).toBe('/review-queue');
  });

  it.each([
    { removed: 'findings.classify', state: 'received', missing: 'classify' },
    { removed: 'findings.classify', state: 'open', missing: 'reclassify' },
  ])('sin $removed no ofrece $missing: la ruta no se abriría', ({ removed, state, missing }) => {
    const permissions = new Set([...COORDINATOR].filter((key) => key !== removed));
    renderActions(state, { permissions });
    expect(
      screen.queryAllByRole('link').map((link) => link.getAttribute('data-access')),
    ).not.toContain(missing);
  });

  it('sin findings.read ni actions.manage, ni lazo ni cola', () => {
    renderActions('open', { permissions: new Set(['findings.classify']) });
    expect(screen.queryAllByRole('link').map((link) => link.getAttribute('data-access'))).toEqual([
      'reclassify',
    ]);
    cleanup();
    const { container } = renderActions('in_review', { permissions: new Set() });
    expect(container.innerHTML).toBe('');
  });

  it('en una fila de la bandeja no repite regiones de navegación', () => {
    render(
      <MemoryRouter>
        <FindingActions findingId={FINDING} state="open" permissions={COORDINATOR} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('navigation')).toBeNull();
    const list = screen.getByRole('list');
    expect(within(list).getAllByRole('link')).toHaveLength(2);
  });
});

describe('catálogo finding_journey (BR-APP-26/27/29/38/41/83)', () => {
  it('ninguna clave nueva tiene un término bloqueado', () => {
    const entries = Object.entries(journey);
    expect(entries.length).toBeGreaterThan(0);
    for (const [key, value] of entries) expect(textViolations(key, value)).toEqual([]);
  });

  it('el aviso de suspensión no promete nada sobre la zona', () => {
    expect(textViolations('finding_journey.suspends', text('finding_journey.suspends'))).toEqual(
      [],
    );
    expect(textViolations('probe.caption', 'Zona segura tras reclasificar')).not.toEqual([]);
  });
});

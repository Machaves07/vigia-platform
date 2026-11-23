/**
 * SCR-08, ficha de vida del hallazgo (TASK-585; O-11 y O-12; BR-APP-54, 77; UI-19 y UI-25).
 *
 * Con MSW y un hallazgo **sintético** que recorre todo el ciclo (recibido, clasificación, acción,
 * dos evidencias, vencimiento, cierre firmado, causa raíz y reapertura con su acción nueva). Las
 * aserciones comparan claves del catálogo, etiquetas de las unidades dueñas, `data-part` y textos
 * exactos; nunca subcadenas de texto con datos (VIG-194).
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import Timeline from '@/pages/scr08/timeline';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { FindingTimeline, TimelineMilestone } from '@/ports/scr08Timeline';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';
import { formatDuration } from '@/texts/duration';
import { label, labelInline } from '@/texts/labels';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { settle } from '../../support/queryCache';
import { meResponse } from '../../support/session';
import { apiError, fixture as detailFixture, ZONE, ZONE_NAME } from './detail_support';

const WAIT = { timeout: 10_000 };
const PLANT = '0190a1b2-0000-7000-8000-000000000001';
const FINDING = '0190a1b2-2222-7000-8000-000000000585';
const START = Date.parse('2026-03-02T15:07:00.000Z');
const MINUTE = 60_000;

type Details = TimelineMilestone['details'];

const NO_DETAILS: Details = {
  segment_index: null,
  delay_ms: null,
  outcome: null,
  reason_category: null,
  reason_text: null,
  is_reclassification: null,
  action_type: null,
  description: null,
  expected_outcome: null,
  assignee_display_name: null,
  due_at: null,
  deadline_days: null,
  performed_at: null,
  performed_by_role: null,
  verification_note: null,
  days_overdue: null,
  escalation_level: null,
  professional_license: null,
  analysis_state: null,
  root_cause_category: null,
  root_cause_statement: null,
  conclusion: null,
  from_state: null,
  compensatory_measure: null,
  review_due_at: null,
  review_note: null,
  detected_at: null,
};

const ACTOR = { role_in_use: 'coordinator_sst', display_name: 'Coordinación SST sintética' };

function iso(offsetMinutes: number): string {
  return new Date(START + offsetMinutes * MINUTE).toISOString();
}

function milestone(
  index: number,
  kind: TimelineMilestone['kind'],
  offsetMinutes: number,
  previousMinutes: number | null,
  changes: Partial<TimelineMilestone> = {},
  details: Partial<Details> = {},
): TimelineMilestone {
  return {
    kind,
    record_type: kind === 'other' ? 'export_produced' : kind,
    record_id: `0190a1b2-7777-7000-8000-${String(index).padStart(12, '0')}`,
    chain_sequence: 100 + index,
    at: iso(offsetMinutes),
    elapsed_ms_since_previous:
      previousMinutes === null ? null : (offsetMinutes - previousMinutes) * MINUTE,
    actor: null,
    details: { ...NO_DETAILS, ...details },
    deadline: null,
    ...changes,
  };
}

/** El ciclo completo, con una reapertura y un vencimiento (datos sintéticos). */
function cycle(): TimelineMilestone[] {
  const at = [0, 37, 71, 1500, 1650, 3200, 4400, 4520, 5900, 5901];
  return [
    milestone(1, 'finding_received', at[0] ?? 0, null, {}, { segment_index: 0 }),
    milestone(
      2,
      'classification',
      at[1] ?? 0,
      at[0] ?? 0,
      { actor: ACTOR, deadline: { due_at: iso(4000), met: true } },
      {
        outcome: 'confirmed',
        reason_category: 'guard_bypassed',
        reason_text: 'Motivo sintético de la clasificación de la zona.',
        is_reclassification: false,
      },
    ),
    milestone(
      3,
      'action_assigned',
      at[2] ?? 0,
      at[1] ?? 0,
      { actor: ACTOR, deadline: { due_at: iso(3000), met: false } },
      {
        action_type: 'engineering',
        description: 'Descripción sintética de la acción sobre la guarda.',
        assignee_display_name: 'Mantenimiento sintético',
        due_at: iso(3000),
        deadline_days: 2,
      },
    ),
    milestone(
      4,
      'action_evidence_added',
      at[3] ?? 0,
      at[2] ?? 0,
      { actor: ACTOR },
      {
        description: 'Evidencia sintética de lo ejecutado en la guarda.',
        performed_at: iso(1400),
        performed_by_role: 'Técnico de mantenimiento',
      },
    ),
    milestone(
      5,
      'action_evidence_added',
      at[4] ?? 0,
      at[3] ?? 0,
      { actor: ACTOR },
      { description: 'Segunda evidencia sintética de la guarda.' },
    ),
    milestone(
      6,
      'action_overdue',
      at[5] ?? 0,
      at[4] ?? 0,
      {},
      { due_at: iso(3000), days_overdue: 1, escalation_level: 'coordinator' },
    ),
    milestone(
      7,
      'closure',
      at[6] ?? 0,
      at[5] ?? 0,
      { actor: ACTOR },
      {
        action_type: 'engineering',
        description: 'Cierre sintético verificado en el acceso.',
        professional_license: 'LIC-SINTETICA-01',
      },
    ),
    milestone(
      8,
      'root_cause_analysis',
      at[7] ?? 0,
      at[6] ?? 0,
      { actor: ACTOR },
      {
        analysis_state: 'signed',
        root_cause_category: 'engineering',
        root_cause_statement: 'Enunciado sintético de la causa raíz.',
        conclusion: 'Conclusión sintética del análisis.',
      },
    ),
    milestone(
      9,
      'reopening',
      at[8] ?? 0,
      at[7] ?? 0,
      { actor: ACTOR },
      { from_state: 'closed', reason_text: 'Motivo sintético de la reapertura de la zona.' },
    ),
    milestone(
      10,
      'action_assigned',
      at[9] ?? 0,
      at[8] ?? 0,
      { actor: ACTOR, deadline: { due_at: iso(9000), met: null } },
      { action_type: 'process', description: 'Acción sintética nueva tras la reapertura.' },
    ),
  ];
}

function timeline(changes: Partial<FindingTimeline> = {}): FindingTimeline {
  return {
    finding_state_id: FINDING,
    subject_kind: 'finding',
    plant_id: PLANT,
    zone_id: ZONE,
    family: 'coexistence',
    lifecycle_state: 'action_assigned',
    time_aggregated: false,
    aggregation_window_minutes: null,
    truncated: false,
    summary: {
      received_at: iso(0),
      closed_at: null,
      business_days_to_close: null,
      classification_met: true,
      action_met: null,
    },
    milestones: cycle(),
    ...changes,
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

async function fixture() {
  const path = `/findings/${FINDING}/timeline`;
  const clock = new FakeClock(START + 10_000 * MINUTE);
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => document.visibilityState === 'visible',
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    permissions: ['findings.read'],
  });
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => path,
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
  const calls = { timeline: 0 };
  const responses = { timeline: (): Response => HttpResponse.json(timeline()) };
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({
        organization: me.organization,
        plants: [
          {
            plant_id: PLANT,
            code: 'PLANTA-SINT',
            name: 'Planta sintética',
            timezone: 'America/Bogota',
            country: 'CO',
            status: 'active',
            data_region: 'co',
            zones: [
              { zone_id: ZONE, plant_id: PLANT, code: 'Z-1', name: ZONE_NAME, node_id: null },
            ],
          },
        ],
        nodes: [],
      }),
    ),
    http.get('http://localhost/loop/findings/:id/timeline', () => {
      calls.timeline++;
      return responses.timeline();
    }),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    return (
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
        </ScreenQueryProvider>
      </SessionProvider>
    );
  }
  const view = () =>
    render(
      <Routes>
        <Route path="/findings/:finding_state_id/timeline" element={<Timeline />} />
        <Route path="/findings/:finding_state_id" element={<p>detalle</p>} />
      </Routes>,
      { wrapper: Wrapper },
    );
  return { clock, cache, calls, responses, view };
}

async function opened(env: Awaited<ReturnType<typeof fixture>>) {
  const view = env.view();
  await screen.findByRole('region', { name: text('scr08.timeline.milestones') }, WAIT);
  await waitFor(() => {
    expect(env.cache.client.isFetching()).toBe(0);
  }, WAIT);
  return view;
}

function items(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>('[data-part="milestone"]')];
}

function part(element: Element, name: string): Element | null {
  return element.querySelector(`[data-part="${name}"]`);
}

describe('SCR-08 ficha de vida del hallazgo (TASK-585)', { timeout: 60_000 }, () => {
  it('se pide una vez por apertura y nunca por refresco en 5 minutos simulados', async () => {
    const env = await fixture();
    await opened(env);
    expect(env.calls.timeline).toBe(1);
    for (let minute = 0; minute < 5; minute += 1) {
      await act(async () => {
        env.clock.advance(MINUTE);
        await settle();
      });
    }
    expect(env.calls.timeline).toBe(1);
  });

  it('un hito por registro, en el orden del servidor, con icono, título por clave y sello', async () => {
    const env = await fixture();
    await opened(env);
    const expected = cycle();
    const shown = items();
    expect(shown.map((li) => li.getAttribute('data-kind'))).toEqual(expected.map((m) => m.kind));
    expect(shown.map((li) => part(li, 'milestone_title')?.textContent)).toEqual(
      expected.map((m) => text(`scr08.timeline.kind.${m.kind}`)),
    );
    expect(shown.map((li) => part(li, 'record_id')?.textContent)).toEqual(
      expected.map((m) => m.record_id),
    );
    expect(shown.every((li) => part(li, 'icon')?.getAttribute('aria-hidden') === 'true')).toBe(
      true,
    );
    // «+2 h 10 min desde el paso anterior», con la duración del servidor; el primero no lo lleva.
    expect(shown.map((li) => part(li, 'elapsed')?.textContent ?? null)).toEqual(
      expected.map((m) =>
        m.elapsed_ms_since_previous === null
          ? null
          : ` · +${formatDuration(m.elapsed_ms_since_previous) ?? ''} ${text('scr08.timeline.elapsed')}`,
      ),
    );
    // La hora en la zona de la planta (UI-25): el instante del servidor, sin recalcular.
    expect(
      shown.map((li) => part(li, 'at')?.querySelector('time')?.getAttribute('dateTime')),
    ).toEqual(expected.map((m) => m.at));
  });

  it('el plazo se dice con texto (a tiempo, fuera de plazo, en curso), no solo con color', async () => {
    const env = await fixture();
    await opened(env);
    const deadlines = items()
      .map((li) => part(li, 'deadline'))
      .filter((node): node is Element => node !== null);
    expect(deadlines.map((node) => node.getAttribute('data-state'))).toEqual([
      'met',
      'late',
      'pending',
    ]);
    expect(deadlines.map((node) => part(node, 'deadline_state')?.textContent)).toEqual([
      text('scr08.timeline.deadline_met'),
      text('scr08.timeline.deadline_late'),
      text('scr08.timeline.deadline_pending'),
    ]);
  });

  it('quién: el usuario de la plataforma y su rol etiquetado, solo donde el registro lo trae', async () => {
    const env = await fixture();
    await opened(env);
    const expected = cycle();
    const shown = items();
    expect(shown.map((li) => part(li, 'actor') !== null)).toEqual(
      expected.map((m) => m.actor !== null),
    );
    const roles = shown
      .map((li) => part(li, 'actor_role'))
      .filter((node): node is Element => node !== null);
    expect(new Set(roles.map((node) => node.textContent))).toEqual(
      new Set([labelInline('role', 'coordinator_sst')]),
    );
    // El responsable de la acción es la instantánea al asignar, en su campo.
    const action = shown[2];
    expect(action && part(action, 'detail_assignee')?.querySelector('dd')?.textContent).toBe(
      'Mantenimiento sintético',
    );
    expect(action && part(action, 'detail_action_type')?.querySelector('dd')?.textContent).toBe(
      label('action_type', 'engineering'),
    );
  });

  it('resumen de un hallazgo cerrado: «Recibido → cerrado en N días hábiles · a tiempo · fuera de plazo»', async () => {
    const env = await fixture();
    env.responses.timeline = () =>
      HttpResponse.json(
        timeline({
          lifecycle_state: 'closed',
          milestones: cycle().slice(0, 8),
          summary: {
            received_at: iso(0),
            closed_at: iso(4400),
            business_days_to_close: 3,
            classification_met: true,
            action_met: false,
          },
        }),
      );
    await opened(env);
    const summary = screen.getByRole('region', { name: text('scr08.timeline.summary') });
    expect(part(summary, 'summary_closed')?.textContent).toBe(
      `${text('scr08.timeline.summary_closed')} 3 ${text('scr08.timeline.business_days')}`,
    );
    expect(part(summary, 'summary_classification')?.textContent).toBe(
      text('scr08.timeline.classification_met'),
    );
    expect(part(summary, 'summary_action')?.textContent).toBe(text('scr08.timeline.action_late'));
    expect(part(summary, 'summary_action')?.getAttribute('data-state')).toBe('late');
  });

  it('resumen de un hallazgo abierto: «sigue abierto» y sin plazo de acción desconocido', async () => {
    const env = await fixture();
    await opened(env);
    const summary = screen.getByRole('region', { name: text('scr08.timeline.summary') });
    expect(part(summary, 'summary_open')?.textContent).toBe(text('scr08.timeline.summary_open'));
    expect(part(summary, 'summary_closed')).toBeNull();
    expect(part(summary, 'summary_action')).toBeNull();
  });

  it('zona unipersonal: la nota de horas redondeadas con su ventana; truncado: lo dice', async () => {
    const env = await fixture();
    env.responses.timeline = () =>
      HttpResponse.json(
        timeline({ time_aggregated: true, aggregation_window_minutes: 60, truncated: true }),
      );
    await opened(env);
    expect(document.querySelector('[data-part="aggregated"]')?.textContent).toBe(
      `${text('scr08.timeline.aggregated')} 60 ${text('scr08.timeline.aggregated_minutes')}`,
    );
    expect(document.querySelector('[data-part="truncated"]')?.textContent).toBe(
      text('scr08.timeline.truncated'),
    );
  });

  it('copia el sello del expediente de un hito', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    disposers.push(() => Reflect.deleteProperty(navigator, 'clipboard'));
    const env = await fixture();
    await opened(env);
    const first = items()[0];
    if (first === undefined) throw new Error('sin hitos');
    const button = part(first, 'copy_record_id');
    if (!(button instanceof HTMLElement)) throw new Error('sin botón de copiar');
    fireEvent.click(button);
    await waitFor(() => {
      expect(part(first, 'copied')?.textContent).toBe(text('scr08.timeline.copied'));
    }, WAIT);
    expect(writeText).toHaveBeenCalledWith(cycle()[0]?.record_id);
  });

  it('fuera de alcance o inexistente: la pantalla uniforme (BR-APP-77)', async () => {
    const env = await fixture();
    env.responses.timeline = () => apiError('not_found', 404);
    env.view();
    await screen.findByText(text('errors.not_available_in_scope'), {}, WAIT);
    expect(items()).toEqual([]);
  });

  it('sin compuerta legible: la sección degradada del detalle con reintentar', async () => {
    const env = await fixture();
    env.responses.timeline = () =>
      apiError('temporarily_unavailable', 503, 'loop_gate_state_unavailable');
    env.view();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-part')).toBe('gate_state_unavailable');
    expect(within(alert).getByText(text('scr08.detail.gate_unavailable'))).toBeDefined();
    expect(items()).toEqual([]);
  });

  it('un fallo transitorio va por UI-19 y «Repetir» vuelve a pedir la ficha', async () => {
    const env = await fixture();
    env.responses.timeline = () => apiError('internal_error', 500);
    env.view();
    const notice = await waitFor(() => {
      const found = document.querySelector('[data-kind][data-code]');
      if (found === null) throw new Error('sin aviso');
      return found;
    }, WAIT);
    expect(notice.getAttribute('data-code')).toBe('internal_error');
    const before = env.calls.timeline;
    env.responses.timeline = () => HttpResponse.json(timeline());
    const retry = notice.querySelector('[data-part="retry"]');
    if (!(retry instanceof HTMLElement)) throw new Error('sin «Repetir»');
    fireEvent.click(retry);
    await screen.findByRole('region', { name: text('scr08.timeline.milestones') }, WAIT);
    expect(env.calls.timeline).toBeGreaterThan(before);
    expect(items()).toHaveLength(cycle().length);
  });

  it('axe sin violaciones en la ficha completa', async () => {
    const env = await fixture();
    const view = await opened(env);
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it('desde el detalle se llega con el enlace «Ver la ficha de vida»', async () => {
    const env = await detailFixture();
    env.view();
    const link = await waitFor(() => {
      const found = document.querySelector('[data-part="timeline_link"]');
      if (found === null) throw new Error('sin enlace');
      return found;
    }, WAIT);
    expect(link.textContent).toBe(text('scr08.timeline.link'));
    expect(link.getAttribute('href')).toBe(
      '/findings/0190a1b2-2222-7000-8000-000000000001/timeline',
    );
  });
});

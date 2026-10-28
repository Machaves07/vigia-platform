/**
 * Proveedores reales y datos exclusivamente sintéticos para SCR-11: sesión con
 * `root_cause.analyze`, transporte con `FakeClock`, enrutador en modo de datos (UI-15 lo exige)
 * y un servidor falso de U-04 con estado (SUP-U05-16 a 20) sobre MSW.
 */
import { cleanup, render } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach } from 'vitest';

import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { Analysis, AssistantRequest, ZoneContext } from '@/ports/scr11RootCause';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';

import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { flush } from '../../support/ports';
import { meResponse } from '../../support/session';

export const INSTANT = '2026-03-02T12:00:00.000Z';
const PLANT = '0190a1b2-0000-7000-8000-000000000101';
const ZONE = '0190a1b2-1111-7000-8000-000000000101';
const FINDING = '0190a1b2-2222-7000-8000-000000000101';
export const ANALYSIS = '0190a1b2-4444-7000-8000-000000000101';
const REQUEST = '0190a1b2-5555-7000-8000-000000000101';
export const RECORD = '0190a1b2-6666-7000-8000-000000000101';
const CORRELATION = '0190a1b2-7777-7000-8000-000000000101';
export const LICENSE = 'LIC-SINT-0042';
const ROUTE = `/findings/${FINDING}/root-cause`;
export const WAIT = { timeout: 10_000 };

export function question(index: number): string {
  return `Pregunta sintética del porqué ${String(index)}`;
}

function zoneContext(): ZoneContext {
  return {
    zone_id: ZONE,
    family: 'coexistence',
    tier: 'tier_1',
    standard_id: '0190a1b2-3333-7000-8000-000000000101',
    standard_version: 3,
    standard_title_es: 'Estándar sintético de coexistencia',
    standard_text: 'Texto declarado sintético del estándar de la zona.',
    observability_state_at_event: 'degraded',
    period: { from: '2026-02-01T00:00:00.000Z', to: INSTANT },
    coverage_summary_period: {
      observable_ms: 3_600_000,
      degraded_ms: 600_000,
      not_observable_ms: 0,
      no_communication_ms: 0,
      never_reported_ms: 0,
      zone_not_active_ms: 0,
    },
    node_state: [{ communication_state: 'online', last_heartbeat_at: INSTANT }],
    signals_by_role: [{ role: 'energy', value: 'asserted', valid: true }],
    episode_durations: { episode_ms: 42_000, condition_ms: 12_000 },
    finding_counts_by_family_in_period: [{ family: 'coexistence', count: 4 }],
    recurrences_in_period: 2,
    previous_actions: [
      {
        action_type: 'engineering',
        outcome: 'closed',
        closed_at: INSTANT,
        recurrence_after_days: 9,
      },
    ],
    previous_cause_categories: [{ category: 'process', count: 1 }],
  };
}

export function analysis(overrides: Partial<Analysis> = {}): Analysis {
  return {
    analysis_id: ANALYSIS,
    subject_kind: 'finding',
    subject_id: FINDING,
    zone_id: ZONE,
    state: 'draft',
    zone_context: zoneContext(),
    why_steps: [],
    next_step_index: 1,
    next_question_es: question(1),
    cause_effect_branches: [],
    root_cause_category: null,
    root_cause_statement: null,
    conclusion: null,
    pending_information: ['why_steps', 'root_cause_category', 'root_cause_statement', 'conclusion'],
    missing_information: null,
    record_id: null,
    signer: null,
    signed_at: null,
    created_at: INSTANT,
    updated_at: INSTANT,
    ...overrides,
  };
}

export function assistantRequest(overrides: Partial<AssistantRequest> = {}): AssistantRequest {
  return {
    request_id: REQUEST,
    analysis_id: ANALYSIS,
    zone_id: ZONE,
    assistant_state: 'pending',
    unavailable_cause: null,
    requested_at: INSTANT,
    resolved_at: null,
    attempt_count: 0,
    draft: null,
    ...overrides,
  };
}

export function readyRequest(overrides: Partial<AssistantRequest['draft'] & object> = {}) {
  return assistantRequest({
    assistant_state: 'ready',
    draft: {
      is_draft: true,
      draft_text: 'Hipótesis sintética del asistente sobre el resguardo de la zona.',
      citations: [
        {
          kind: 'record',
          id: 'R1',
          quoted_fact_es: 'Hecho citado sintético del expediente',
          reference_id: RECORD,
        },
        {
          kind: 'coverage',
          id: 'C1',
          quoted_fact_es: 'Tramo degradado sintético',
          reference_id: null,
        },
      ],
      insufficient_evidence_declared: false,
      proposed_cause_category: 'engineering',
      model: 'modelo-sintetico',
      model_version: '2026-01',
      created_at: INSTANT,
      ...overrides,
    },
  });
}

export function apiError(
  code: string,
  status: number,
  detail_code: string | null = null,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      code,
      detail_code,
      message_es: 'Mensaje sintético del servidor.',
      correlation_id: CORRELATION,
      retry_after_seconds: null,
      ...extra,
    },
    { status },
  );
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Respuesta guionizada de una consulta de estado del asistente. */
type StatusStep = () => Response | Promise<Response>;

/** Servidor falso de U-04 con estado: valida el orden de los porqués y la firma. */
export function fakeLoop() {
  const calls = {
    start: 0,
    inProgress: 0,
    steps: [] as { index: number; body: Record<string, unknown> }[],
    signatures: [] as Record<string, unknown>[],
    assistantRequests: 0,
    statusAt: [] as number[],
  };
  let current = analysis();
  /** Hay un análisis creado (por `start` o sembrado con `saved`) y su última solicitud. */
  let started = false;
  let lastRequest: string | null = null;
  const state = {
    startResponse: null as (() => Response) | null,
    inProgressResponse: null as (() => Response) | null,
    stepResponse: null as ((index: number) => Response | null) | null,
    signatureResponse: null as (() => Response | null) | null,
    assistantResponse: null as (() => Response) | null,
    statusSteps: [] as StatusStep[],
  };
  return {
    calls,
    state,
    get analysis() {
      return current;
    },
    /** Siembra un análisis guardado sin terminar (VIG-363) y, si se da, su última solicitud. */
    saved(value: Analysis, requestId: string | null = null) {
      current = value;
      started = true;
      lastRequest = requestId;
    },
    install(clock: FakeClock) {
      server.use(
        http.post('http://localhost/loop/findings/:id/root-cause-analyses', () => {
          calls.start += 1;
          const scripted = state.startResponse?.();
          if (scripted !== undefined) return scripted;
          started = true;
          lastRequest = null;
          return HttpResponse.json(current, { status: 201 });
        }),
        http.get('http://localhost/loop/findings/:id/root-cause-analyses/in-progress', () => {
          calls.inProgress += 1;
          return (
            state.inProgressResponse?.() ??
            HttpResponse.json({
              analysis: started && current.state !== 'signed' ? current : null,
              assistant_request_id: started && current.state !== 'signed' ? lastRequest : null,
            })
          );
        }),
        http.put(
          'http://localhost/loop/root-cause-analyses/:id/steps/:index',
          async ({ params, request }) => {
            const index = Number(params.index);
            const body = (await request.json()) as Record<string, unknown>;
            calls.steps.push({ index, body });
            const scripted = state.stepResponse?.(index) ?? null;
            if (scripted !== null) return scripted;
            if (index > current.why_steps.length + 1)
              return apiError('conflict', 409, 'loop_analysis_step_out_of_order');
            const step = {
              step_index: index,
              question_es: question(index),
              statement: str(body.statement),
              evidence_refs: [],
              category: typeof body.category === 'string' ? body.category : null,
            };
            const why_steps = [
              ...current.why_steps.filter((s) => s.step_index !== index),
              step,
            ].sort((a, b) => a.step_index - b.step_index);
            const next = why_steps.length < 5 ? why_steps.length + 1 : null;
            current = {
              ...current,
              why_steps,
              next_step_index: next,
              next_question_es: next === null ? null : question(next),
              pending_information: current.pending_information.filter((i) => i !== 'why_steps'),
              updated_at: new Date(clock.now()).toISOString(),
            };
            return HttpResponse.json(current);
          },
        ),
        http.post(
          'http://localhost/loop/root-cause-analyses/:id/signature',
          async ({ request }) => {
            const body = (await request.json()) as Record<string, unknown>;
            calls.signatures.push(body);
            const scripted = state.signatureResponse?.() ?? null;
            if (scripted !== null) return scripted;
            if (current.state === 'signed')
              return apiError('conflict', 409, 'loop_transition_not_allowed');
            if (body.professional_license == null)
              return apiError('invalid_request', 400, 'loop_signer_license_missing');
            current = {
              ...current,
              state: 'signed',
              root_cause_category: str(body.root_cause_category),
              root_cause_statement: str(body.root_cause_statement),
              conclusion: str(body.conclusion),
              pending_information: [],
              record_id: RECORD,
              signer: {
                user_id: '0190a1b2-0000-7000-8000-00000000000a',
                display_name: 'Persona sintética',
                professional_license: str(body.professional_license),
              },
              signed_at: INSTANT,
            };
            return HttpResponse.json(current, { status: 201 });
          },
        ),
        http.post('http://localhost/loop/root-cause-analyses/:id/assistant-requests', () => {
          calls.assistantRequests += 1;
          const scripted = state.assistantResponse?.();
          if (scripted !== undefined) return scripted;
          lastRequest = assistantRequest().request_id;
          return HttpResponse.json(assistantRequest(), { status: 202 });
        }),
        http.get('http://localhost/loop/assistant-requests/:id', () => {
          calls.statusAt.push(clock.now());
          const step = state.statusSteps.shift();
          return step === undefined ? HttpResponse.json(assistantRequest()) : step();
        }),
      );
    },
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

/** Sesión, caché, transporte y jerarquía sintéticos; `license: null` es un perfil sin licencia. */
export async function providers({
  license = LICENSE,
  permissions = ['root_cause.analyze', 'findings.read'],
}: { license?: string | null; permissions?: string[] } = {}) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => true,
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  const me = {
    ...meResponse({
      idleExpiresAt: clock.now() + 1_800_000,
      absoluteExpiresAt: clock.now() + 8 * 3_600_000,
      permissions,
    }),
    professional_license: license,
  };
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => ROUTE,
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
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
              {
                zone_id: ZONE,
                plant_id: PLANT,
                code: 'Z-1',
                name: 'Zona sintética',
                node_id: null,
              },
            ],
          },
        ],
        nodes: [],
      }),
    ),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  function Providers({ children }: { readonly children: ReactNode }) {
    return (
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>{children}</ScreenQueryProvider>
      </SessionProvider>
    );
  }
  return { clock, cache, Providers };
}

/** Pinta `element` en la ruta de SCR-11 con un enrutador de datos (UI-15 usa `useBlocker`). */
export function renderAt(
  element: ReactNode,
  Providers: (props: { readonly children: ReactNode }) => ReactNode,
) {
  const router = createMemoryRouter(
    [
      { path: '/findings/:finding_state_id/root-cause', element },
      { path: '*', element: <p>Otra pantalla</p> },
    ],
    { initialEntries: [ROUTE] },
  );
  return render(
    <Providers>
      <RouterProvider router={router} />
    </Providers>,
  );
}

/** Avanza el reloj simulado en pasos de `stepMs`, dejando correr las promesas entre pasos. */
export async function advance(clock: FakeClock, ms: number, stepMs = 1_000): Promise<void> {
  for (let elapsed = 0; elapsed < ms; elapsed += stepMs) {
    clock.advance(Math.min(stepMs, ms - elapsed));
    await flush();
  }
}

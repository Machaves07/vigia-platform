/**
 * Apoyo de las pruebas de SCR-06 (VIG-310): datos sintéticos, reloj simulado, transporte con MSW
 * y programador manual de fotogramas. Ningún dato de persona real (P3).
 */
import { render, type RenderResult } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { ReactElement } from 'react';
import { expect } from 'vitest';

import { createTransport, installTransport, createPortConnection } from '@/ports/base';
import type {
  CommissioningClip,
  CommissioningClips,
  CommissioningRecord,
  CurrentWalkTest,
  MatrixRow,
  OcclusionTest,
  PassOut,
  WalkTestSession,
  WalkTestStep,
} from '@/ports/scr06Commissioning';
import { createScreenQueryCache, ScreenQueryProvider, type ScreenQueryCache } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { mswPath } from '../../msw/handlers';
import { FakeClock } from '../../support/fakeClock';

/** Nombre accesible del botón «Fila n» de la matriz: empieza por su texto visible (WCAG 2.5.3). */
export function selectRowName(posture: string, number: number): string {
  return `${text('scr06.matrix.row')} ${String(number)}: ${text('scr06.matrix.select')}, ${label('posture', posture)}`;
}

export const ZONE_ID = '0190a1b2-0000-7000-8000-000000000001';
export const SESSION_ID = '0190a1b2-0000-7000-8000-000000000002';
const NODE_ID = '0190a1b2-0000-7000-8000-000000000003';
export const ROW_STANDING = '0190a1b2-0000-7000-8000-000000000004';
export const ROW_CROUCHED = '0190a1b2-0000-7000-8000-000000000005';
export const STEP_OPEN = '0190a1b2-0000-7000-8000-000000000006';
export const STEP_CLOSED = '0190a1b2-0000-7000-8000-000000000007';
export const PASS_ID = '0190a1b2-0000-7000-8000-000000000008';
export const CLIP_A = '0190a1b2-0000-7000-8000-000000000009';
export const CLIP_B = '0190a1b2-0000-7000-8000-00000000000a';
export const RESPONSIBLE = '0190a1b2-0000-7000-8000-00000000000b';

/** `started_at` del servidor de las pruebas: 3 h antes del reloj del navegador (desfase a propósito). */
const SERVER_STARTED_AT = '2026-10-24T10:00:00.000Z';
const SERVER_STARTED_MS = Date.parse(SERVER_STARTED_AT);
/** Reloj del navegador de las pruebas: 3 h por delante del servidor. */
export const BROWSER_NOW_MS = SERVER_STARTED_MS + 3 * 3_600_000;

export function row(overrides: Partial<MatrixRow> = {}): MatrixRow {
  return {
    row_id: ROW_STANDING,
    standard_id: '0190a1b2-0000-7000-8000-00000000000c',
    standard_version: 7,
    // Gramática del predicado del contrato: presencia + rol de señal afirmado.
    predicate_conditions: [{ presence: true }, { signal_role: 'energy', value: 'asserted' }],
    posture: 'standing',
    required_passes: 3,
    passes: { detected: 1, missed: 0, false_alarm: 0 },
    ...overrides,
  };
}

export function step(overrides: Partial<WalkTestStep> = {}): WalkTestStep {
  return {
    step_id: STEP_OPEN,
    session_id: SESSION_ID,
    step_kind: 'walk_test_passes',
    responsible_user_id: RESPONSIBLE,
    started_at: SERVER_STARTED_AT,
    ended_at: null,
    correction: null,
    effective_duration_ms: null,
    ...overrides,
  };
}

export function pass(overrides: Partial<PassOut> = {}): PassOut {
  return {
    pass_id: PASS_ID,
    session_id: SESSION_ID,
    row_id: ROW_STANDING,
    result: 'detected',
    evidence_ref: CLIP_A,
    recorded_by: RESPONSIBLE,
    recorded_at: '2026-10-24T10:05:00.000Z',
    ...overrides,
  };
}

export function session(overrides: Partial<WalkTestSession> = {}): WalkTestSession {
  return {
    session_id: SESSION_ID,
    zone_id: ZONE_ID,
    node_id: NODE_ID,
    catalog_version: 12,
    kind: 'initial',
    status: 'in_progress',
    passes_per_cell: 3,
    rows: [row(), row({ row_id: ROW_CROUCHED, posture: 'crouched', required_passes: 4 })],
    started_at: SERVER_STARTED_AT,
    last_activity_at: '2026-10-24T10:05:00.000Z',
    closed_at: null,
    reopened_at: null,
    reopened_by: null,
    reopen_reason_es: null,
    steps: [step()],
    passes: [],
    total_duration_ms: 5_400_000,
    steps_summary: [
      { step_kind: 'walk_test_passes', duration_ms: 3_600_000 },
      { step_kind: 'framing', duration_ms: 1_800_000 },
    ],
    occlusion_tests: [],
    ...overrides,
  };
}

export const CAMERA_A = '0190a1b2-0000-7000-8000-00000000000d';
const CAMERA_B = '0190a1b2-0000-7000-8000-00000000000e';
export const OCCLUSION_TEST_ID = '0190a1b2-0000-7000-8000-00000000000f';
export const RECORD_ID = '0190a1b2-0000-7000-8000-000000000010';
export const SIGNER_ID = '0190a1b2-0000-7000-8000-000000000011';
export const COSIGNER_ID = '0190a1b2-0000-7000-8000-000000000012';
export const EVENT_ID = '0190a1b2-0000-7000-8000-000000000013';

/** Prueba de oclusión sintética: ventana de 2 min y plazo = fin + 5 min (hora del servidor). */
export function occlusionTest(overrides: Partial<OcclusionTest> = {}): OcclusionTest {
  return {
    test_id: OCCLUSION_TEST_ID,
    camera_id: CAMERA_A,
    started_at: '2026-10-24T10:03:00.000Z',
    ended_at: '2026-10-24T10:05:00.000Z',
    deadline: '2026-10-24T10:10:00.000Z',
    verification: 'pending',
    correlated_event_ids: [],
    declared_reason_es: null,
    failure_reason: null,
    recorded_by: RESPONSIBLE,
    ...overrides,
  };
}

/** Acta cerrada sintética: dos filas, dos cámaras, tramos medidos y sin medir. */
export function commissioningRecord(
  overrides: Partial<CommissioningRecord> = {},
): CommissioningRecord {
  return {
    commissioning_record_id: RECORD_ID,
    session_id: SESSION_ID,
    zone_id: ZONE_ID,
    catalog_version: 12,
    kind: 'initial',
    passes_per_cell: 3,
    matrix_results: [
      { row_id: ROW_STANDING, detected: 3, missed: 0, false_alarms: 1 },
      { row_id: ROW_CROUCHED, detected: 4, missed: 0, false_alarms: 0 },
    ],
    false_negatives_total: 0,
    false_alarm_rate_observed: 0.125,
    false_alarm_threshold: 0.2,
    false_alarm_acceptance: null,
    latency: {
      node_tranche: { p95_ms: 180, measured_by: 'installer' },
      platform_tranche: {
        median_ms: 90,
        p95_ms: 140,
        max_ms: 210,
        repetitions: 120,
        measured_by: 'platform',
      },
      exposure_tranche: null,
      served_tranche: null,
      not_measured: ['exposure_tranche', 'served_tranche'],
      indicative_sum_median_ms: null,
      indicative_sum_p95_ms: 320,
      indicative: true,
      repetitions_counted: 120,
    },
    cameras_measured: [
      { camera_id: CAMERA_A, measured_fps: 14.5, declared_min_fps: 10 },
      { camera_id: CAMERA_B, measured_fps: null, declared_min_fps: null },
    ],
    occlusion_summary: [
      { camera_id: CAMERA_A, verification: 'verified', test_id: OCCLUSION_TEST_ID },
      { camera_id: CAMERA_B, verification: 'declared', test_id: null },
    ],
    total_duration_ms: 5_400_000,
    steps_summary: [
      { step_kind: 'walk_test_passes', duration_ms: 3_600_000 },
      { step_kind: 'framing', duration_ms: 1_800_000 },
    ],
    installer_measurements: {
      beacon_latency_ms_p95: 180,
      measured_by: 'installer',
      baselines: [
        { camera_id: CAMERA_A, zone_id: ZONE_ID, captured_at: '2026-10-24T09:30:00.000Z' },
      ],
    },
    signatures: [
      {
        user_id: SIGNER_ID,
        role_in_use: 'provider_installer',
        signed_at: '2026-10-24T11:00:00.000Z',
      },
    ],
    closed_at: '2026-10-24T11:00:00.000Z',
    ...overrides,
  };
}

export function currentWalkTest(value: WalkTestSession | null): CurrentWalkTest {
  return { zone_id: ZONE_ID, session: value };
}

export function clip(overrides: Partial<CommissioningClip> = {}): CommissioningClip {
  return {
    clip_id: CLIP_A,
    zone_id: ZONE_ID,
    node_id: NODE_ID,
    received_at: '2026-10-24T09:59:00.000Z',
    sha256: 'a'.repeat(64),
    blur_check_result: null,
    ...overrides,
  };
}

export function clipsPage(clips: readonly CommissioningClip[]): CommissioningClips {
  return { clips: [...clips], next_after: null };
}

/** Manejador de la sesión vigente, con el valor que devuelve. */
export function currentWalkTestHandler(value: CurrentWalkTest) {
  return http.get(mswPath('/zones/:zone_id/walk-tests/current'), () =>
    HttpResponse.json(value as unknown as Record<string, unknown>),
  );
}

/** Manejador del selector de clips. */
export function clipsHandler(value: CommissioningClips) {
  return http.get(mswPath('/zones/:zone_id/commissioning-clips'), () =>
    HttpResponse.json(value as unknown as Record<string, unknown>),
  );
}

/** Petición de escritura capturada: método, ruta, cuerpo y cabeceras. */
export interface CapturedRequest {
  readonly method: string;
  readonly url: string;
  readonly body: unknown;
}

/**
 * Manejador que captura las peticiones de una operación y responde con `response` (o un error de
 * `code`). El cuerpo se guarda ya parseado para compararlo por campos, nunca por subcadenas.
 */
export function captureHandler(options: {
  readonly method: 'get' | 'post' | 'put' | 'delete';
  readonly path: string;
  readonly calls: CapturedRequest[];
  readonly response: () => Response;
}) {
  const handler = http[options.method](mswPath(options.path), async ({ request }) => {
    const text = await request.text();
    options.calls.push({
      method: request.method,
      url: new URL(request.url).pathname,
      body: text === '' ? null : (JSON.parse(text) as unknown),
    });
    return options.response();
  });
  return handler;
}

export interface TestSession {
  readonly clock: FakeClock;
  readonly cache: ScreenQueryCache;
  readonly result: RenderResult;
}

/**
 * Reloj simulado, transporte con MSW y caché de consultas, con la pantalla montada. La pantalla
 * recibe el mismo reloj que el transporte y la caché: el desfase con el servidor es el de las
 * marcas de los datos sintéticos.
 */
export function renderScreen(
  screen: (clock: FakeClock) => ReactElement,
  options: { readonly nowMs?: number; readonly clock?: FakeClock } = {},
): TestSession {
  const clock = options.clock ?? new FakeClock(options.nowMs ?? BROWSER_NOW_MS);
  const connection = createPortConnection(clock);
  installTransport(createTransport({ clock, connection, devLog: () => undefined }));
  const gates = {
    isVisible: () => true,
    canReadPlatform: () => true,
    isActive: () => true,
  };
  const cache = createScreenQueryCache({ clock, gates });
  const result = render(<ScreenQueryProvider cache={cache}>{screen(clock)}</ScreenQueryProvider>);
  return { clock, cache, result };
}

/** Programador manual de fotogramas: nada se ejecuta hasta `run()`. */
export function frameScheduler() {
  const pending: (() => void)[] = [];
  return {
    nextFrame: (callback: () => void) => {
      pending.push(callback);
    },
    pendingCount: () => pending.length,
    run() {
      for (const callback of pending.splice(0, pending.length)) callback();
    },
  };
}

/** Cierra el transporte instalado al terminar cada prueba. */
export function resetTransport(): void {
  installTransport(null);
}

/** Un aviso de la pila con la clave de texto esperada. */
export function textKeyOf(element: Element | null): string | null {
  return element?.getAttribute('data-text-key') ?? null;
}

export function expectExactKeys(body: unknown, keys: readonly string[]): void {
  expect(Object.keys(body as Record<string, unknown>).sort()).toEqual([...keys].sort());
}

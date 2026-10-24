/**
 * Pasos del comisionamiento (IF-13 paso 2; RV-09; BR-APP-45; H-53).
 *
 * - El cronómetro parte del `started_at` del servidor aunque el reloj del navegador vaya 3 h por
 *   delante: no mide desde el montaje ni desde la hora de pared del navegador.
 * - Abrir y cerrar pasos envía exactamente el cuerpo del contrato; un motivo vacío no sale y un
 *   identificador de responsable inválido lo detiene el espejo del esquema, no el servidor.
 * - Las horas se muestran **por zona** (el desglose que da el servidor), nunca agregadas por
 *   responsable (H-53).
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SessionScreen } from '@/pages/scr06/session/SessionScreen';
import type { WalkTestSession } from '@/ports/scr06Commissioning';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { server } from '../../msw/server';
import { expectExactKeys, textKeyOf } from './support';
import {
  RESPONSIBLE,
  SESSION_ID,
  STEP_CLOSED,
  STEP_OPEN,
  ZONE_ID,
  captureHandler,
  currentWalkTest,
  currentWalkTestHandler,
  resetTransport,
  renderScreen,
  session,
  step,
  type CapturedRequest,
  type TestSession,
} from './support';

const REQUIRED = `(${text('components_forms.reason.required')})`;
const RESPONSIBLE_LABEL = `${text('scr06.steps.responsible')} ${REQUIRED}`;
const OPEN_LABEL = text('scr06.steps.open.submit');
const CLOSE_LABEL = text('scr06.steps.close');
const CORRECTION_REASON_LABEL = `${text('scr06.steps.correction.reason')} ${REQUIRED}`;

/** Sesión con un paso abierto (el que lleva el cronómetro) y otro cerrado. */
function sessionWithSteps(): WalkTestSession {
  return session({
    steps: [
      step(),
      step({
        step_id: STEP_CLOSED,
        step_kind: 'framing',
        started_at: '2026-10-24T09:00:00.000Z',
        ended_at: '2026-10-24T09:30:00.000Z',
        effective_duration_ms: 1_800_000,
      }),
    ],
    // Un solo desglose del servidor para el tipo de paso compartido: si la pantalla agregara por
    // responsable habría dos entradas (H-53).
    steps_summary: [{ step_kind: 'walk_test_passes', duration_ms: 7_200_000 }],
    total_duration_ms: 9_000_000,
  });
}

let mounted: TestSession | null = null;
let openCalls: CapturedRequest[] = [];
let closeCalls: CapturedRequest[] = [];

beforeEach(() => {
  openCalls = [];
  closeCalls = [];
  server.use(
    currentWalkTestHandler(currentWalkTest(sessionWithSteps())),
    captureHandler({
      method: 'post',
      path: '/walk-tests/{session_id}/steps',
      calls: openCalls,
      response: () => HttpResponse.json(step(), { status: 201 }),
    }),
    captureHandler({
      method: 'post',
      path: '/walk-tests/{session_id}/steps/{step_id}/close',
      calls: closeCalls,
      response: () => HttpResponse.json(step({ ended_at: '2026-10-24T13:00:00.000Z' })),
    }),
  );
});

afterEach(() => {
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

async function openScreen(): Promise<TestSession> {
  const rendered = renderScreen((clock) => <SessionScreen zoneId={ZONE_ID} clock={clock} />);
  mounted = rendered;
  await screen.findByRole('timer');
  return rendered;
}

function expandCloseForm(): void {
  fireEvent.click(screen.getByRole('button', { name: CLOSE_LABEL }));
}

function closeWithoutCorrection(): void {
  const button = document.querySelector<HTMLButtonElement>(
    '[data-part="close_without_correction"]',
  );
  if (button === null) throw new Error('falta el botón de cierre sin corrección');
  fireEvent.click(button);
}

function closeWithCorrection(): void {
  const button = document.querySelector<HTMLButtonElement>('[data-part="close_with_correction"]');
  if (button === null) throw new Error('falta el botón de cierre con corrección');
  fireEvent.click(button);
}

describe('cronómetro de presentación (BR-APP-45)', () => {
  it('parte del started_at del servidor con el reloj del navegador 3 h por delante', async () => {
    const { clock } = await openScreen();
    const stopwatch = screen.getByRole('timer');
    expect(stopwatch.getAttribute('data-elapsed-ms')).toBe(String(3 * 3_600_000));
    expect(stopwatch.textContent).toBe('03:00:00');

    act(() => {
      clock.advance(1_000);
    });
    expect(screen.getByRole('timer').getAttribute('data-elapsed-ms')).toBe(
      String(3 * 3_600_000 + 1_000),
    );
    expect(screen.getByRole('timer').textContent).toBe('03:00:01');
  });

  it('solo el paso abierto lleva cronómetro; el cerrado muestra su duración efectiva', async () => {
    await openScreen();
    expect(screen.getAllByRole('timer')).toHaveLength(1);
    const durations = Array.from(
      document.querySelectorAll('[data-part="step_duration"]'),
      (cell) => cell.textContent,
    );
    expect(durations).toContain('0 h 30 min 00 s');
  });
});

describe('abrir un paso', () => {
  it('envía tipo y responsable con las claves del contrato', async () => {
    await openScreen();
    fireEvent.change(screen.getByRole('combobox', { name: text('scr06.steps.kind') }), {
      target: { value: 'walk_test_passes' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: RESPONSIBLE_LABEL }), {
      target: { value: RESPONSIBLE },
    });
    fireEvent.click(screen.getByRole('button', { name: OPEN_LABEL }));
    await waitFor(() => {
      expect(openCalls).toHaveLength(1);
    });
    expect(openCalls[0]?.url).toBe(`/walk-tests/${SESSION_ID}/steps`);
    expectExactKeys(openCalls[0]?.body, ['step_kind', 'responsible_user_id']);
    expect(openCalls[0]?.body).toEqual({
      step_kind: 'walk_test_passes',
      responsible_user_id: RESPONSIBLE,
    });
  });

  it('un responsable inválido lo detiene el espejo del esquema y no sale ninguna petición', async () => {
    await openScreen();
    fireEvent.change(screen.getByRole('textbox', { name: RESPONSIBLE_LABEL }), {
      target: { value: 'no-es-un-uuid' },
    });
    fireEvent.click(screen.getByRole('button', { name: OPEN_LABEL }));
    await waitFor(() => {
      expect(textKeyOf(document.querySelector('[data-part="local_error"]'))).toBe(
        'validation.format_uuid',
      );
    });
    expect(openCalls).toHaveLength(0);
  });
});

describe('cerrar un paso', () => {
  it('cierra sin corrección enviando el motivo nulo', async () => {
    await openScreen();
    expandCloseForm();
    closeWithoutCorrection();
    await waitFor(() => {
      expect(closeCalls).toHaveLength(1);
    });
    expect(closeCalls[0]?.url).toBe(`/walk-tests/${SESSION_ID}/steps/${STEP_OPEN}/close`);
    expect(closeCalls[0]?.body).toEqual({ correction: null });
  });

  it('una corrección sin motivo no sale y marca el campo', async () => {
    await openScreen();
    expandCloseForm();
    closeWithCorrection();
    await waitFor(() => {
      expect(
        Array.from(document.querySelectorAll('[data-part="local_error"]')).map(textKeyOf),
      ).toContain('validation.required');
    });
    expect(closeCalls).toHaveLength(0);
  });

  it('una corrección con motivo y horas envía el cuerpo exacto', async () => {
    await openScreen();
    expandCloseForm();
    fireEvent.change(screen.getByRole('textbox', { name: CORRECTION_REASON_LABEL }), {
      target: { value: 'El paso se cerró antes de terminar el recorrido.' },
    });
    fireEvent.change(
      screen.getByRole('textbox', { name: text('scr06.steps.correction.ended_at') }),
      { target: { value: '2026-10-24T11:00:00.000Z' } },
    );
    closeWithCorrection();
    await waitFor(() => {
      expect(closeCalls).toHaveLength(1);
    });
    expect(closeCalls[0]?.body).toEqual({
      correction: {
        started_at: null,
        ended_at: '2026-10-24T11:00:00.000Z',
        reason_es: 'El paso se cerró antes de terminar el recorrido.',
      },
    });
  });
});

describe('horas por zona (H-53)', () => {
  it('muestra el desglose del servidor por tipo de paso y el total de la zona', async () => {
    await openScreen();
    const heading = screen.getByRole('heading', { name: text('scr06.hours.title') });
    const panel = heading.closest('section');
    if (panel === null) throw new Error('falta la sección de horas');
    const items = within(panel).getAllByRole('listitem');
    expect(items).toHaveLength(1);
    expect(items[0]?.textContent).toBe(`${label('step_kind', 'walk_test_passes')} 2 h 00 min 00 s`);
    expect(within(panel).getByText(new RegExp(`2 h 30 min 00 s$`, 'u'))).toBeTruthy();
  });
});

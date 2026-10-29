/**
 * Consola de comisionamiento: sesión y pasos (IF-13 paso 1; RV-09).
 *
 * - Retoma la sesión en curso con las marcas del servidor y, si no hay ninguna, la abre con el
 *   número de pases por celda que viaja en el cuerpo exacto del contrato.
 * - Una sesión incompleta solo continúa con motivo; el aviso y la etiqueta del `detail_code` los
 *   da el mapa cerrado de errores, no la pantalla.
 * - Los estados comunes son `ScreenState`: el error lleva el `correlation_id` del servidor.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { SessionScreen } from '@/pages/scr06/session/SessionScreen';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { apiError, mswPath } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  SESSION_ID,
  ZONE_ID,
  captureHandler,
  currentWalkTest,
  currentWalkTestHandler,
  resetTransport,
  row,
  renderScreen,
  session,
  type CapturedRequest,
  type TestSession,
} from './support';

const CORRELATION_ID = '0190a1b2-0000-7000-8000-0000000000ff';
const REQUIRED = `(${text('components_forms.reason.required')})`;
const REOPEN_REASON_LABEL = `${text('scr06.session.reopen.reason')} ${REQUIRED}`;
const OPEN_SESSION_LABEL = text('scr06.session.open.submit');
const REOPEN_LABEL = text('scr06.session.reopen.submit');
const PASSES_LABEL = `${text('scr06.session.passes_per_cell')} ${REQUIRED}`;

let mounted: TestSession | null = null;

beforeEach(() => {
  mounted = null;
});

afterEach(() => {
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

function openScreen(): TestSession {
  const rendered = renderScreen((clock) => <SessionScreen zoneId={ZONE_ID} clock={clock} />);
  mounted = rendered;
  return rendered;
}

describe('sesión en curso', () => {
  it('la retoma desde la ruta de la zona y muestra las marcas del servidor', async () => {
    const reads: CapturedRequest[] = [];
    server.use(
      captureHandler({
        method: 'get',
        path: '/zones/{zone_id}/walk-tests/current',
        calls: reads,
        response: () => HttpResponse.json(currentWalkTest(session())),
      }),
    );
    openScreen();
    const status = await screen.findByText(label('walk_test_status', 'in_progress'), {
      selector: '[data-part="session_status"]',
    });
    expect(status).toBeTruthy();
    expect(reads).toHaveLength(1);
    expect(reads[0]?.url).toBe(`/zones/${ZONE_ID}/walk-tests/current`);
    // Las marcas son las del servidor (el instante exacto en `dateTime`), con la zona nombrada
    // junto a la hora (VIG-366): fuera del armazón, UTC.
    const instants = Array.from(
      document.querySelectorAll('[data-part="server_instant"] time'),
      (node) => node.getAttribute('dateTime'),
    ).filter((instant) => instant === '2026-10-24T10:00:00.000Z');
    expect(instants.length).toBeGreaterThan(0);
    expect(screen.getByText(String(session().catalog_version))).toBeTruthy();
  });
});

describe('abrir la sesión', () => {
  it('envía el número de pases por celda y retoma la sesión creada', async () => {
    const openCalls: CapturedRequest[] = [];
    let opened = false;
    server.use(
      http.get(mswPath('/zones/:zone_id/walk-tests/current'), () =>
        HttpResponse.json(currentWalkTest(opened ? session() : null)),
      ),
      captureHandler({
        method: 'post',
        path: '/zones/{zone_id}/walk-tests',
        calls: openCalls,
        response: () => {
          opened = true;
          return HttpResponse.json(session(), { status: 201 });
        },
      }),
    );
    openScreen();
    const passes = await screen.findByRole('spinbutton', { name: PASSES_LABEL });
    fireEvent.change(passes, { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: OPEN_SESSION_LABEL }));
    await waitFor(() => {
      expect(openCalls).toHaveLength(1);
    });
    expect(openCalls[0]?.url).toBe(`/zones/${ZONE_ID}/walk-tests`);
    expect(openCalls[0]?.body).toEqual({ passes_per_cell: 4 });
    // La escritura invalida la lectura de la sesión: la consola la retoma sin recargar.
    expect(
      await screen.findByText(label('walk_test_status', 'in_progress'), {
        selector: '[data-part="session_status"]',
      }),
    ).toBeTruthy();
  });

  it('sin sesión y con el puerto en línea muestra el estado vacío de la pantalla', async () => {
    server.use(currentWalkTestHandler(currentWalkTest(null)));
    openScreen();
    expect(await screen.findByText(text('scr06.session.empty'))).toBeTruthy();
  });
});

describe('sesión incompleta', () => {
  beforeEach(() => {
    server.use(
      currentWalkTestHandler(
        currentWalkTest(
          session({
            status: 'incomplete',
            last_activity_at: '2026-10-17T08:00:00.000Z',
            closed_at: null,
          }),
        ),
      ),
    );
  });

  it('muestra desde cuándo está incompleta y no reabre sin motivo', async () => {
    const reopenCalls: CapturedRequest[] = [];
    server.use(
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/reopen',
        calls: reopenCalls,
        response: () => HttpResponse.json(session({ status: 'reopened' })),
      }),
    );
    openScreen();
    expect((await screen.findAllByText(/2026-10-17T08:00:00\.000Z/u)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: REOPEN_LABEL }));
    await waitFor(() => {
      expect(
        document.querySelector('[data-part="local_error"]')?.getAttribute('data-text-key'),
      ).toBe('validation.required');
    });
    expect(reopenCalls).toHaveLength(0);
  });

  it('reabre con el motivo escrito', async () => {
    const reopenCalls: CapturedRequest[] = [];
    server.use(
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/reopen',
        calls: reopenCalls,
        response: () => HttpResponse.json(session({ status: 'reopened' })),
      }),
    );
    openScreen();
    fireEvent.change(await screen.findByRole('textbox', { name: REOPEN_REASON_LABEL }), {
      target: { value: 'La sesión quedó abierta por una parada de planta.' },
    });
    fireEvent.click(screen.getByRole('button', { name: REOPEN_LABEL }));
    await waitFor(() => {
      expect(reopenCalls).toHaveLength(1);
    });
    expect(reopenCalls[0]?.url).toBe(`/walk-tests/${SESSION_ID}/reopen`);
    expect(reopenCalls[0]?.body).toEqual({
      reason_es: 'La sesión quedó abierta por una parada de planta.',
    });
  });
});

describe('estados comunes', () => {
  it('axe no encuentra violaciones serias en la consola de sesión', async () => {
    server.use(currentWalkTestHandler(currentWalkTest(session())));
    const { result } = openScreen();
    await screen.findByRole('timer');
    const results = await axe(result.container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });

  it('un fallo del servidor muestra el error con su correlation_id', async () => {
    server.use(
      http.get(mswPath('/zones/:zone_id/walk-tests/current'), () =>
        HttpResponse.json(
          {
            code: 'internal_error',
            message_es: 'Algo falló.',
            correlation_id: CORRELATION_ID,
            detail_code: null,
            retry_after_seconds: null,
          },
          { status: 500 },
        ),
      ),
    );
    openScreen();
    const code = await screen.findByText(CORRELATION_ID);
    expect(code.tagName).toBe('CODE');
    expect(screen.getByRole('button', { name: text('screen_state.retry') })).toBeTruthy();
  });

  it('una sesión rechazada por la compuerta de montaje muestra la etiqueta del detail_code', async () => {
    server.use(
      currentWalkTestHandler(currentWalkTest(null)),
      http.post(mswPath('/zones/:zone_id/walk-tests'), () =>
        apiError('conflict', 409, { detailCode: 'catalog_mounting_gate_pending' }),
      ),
    );
    openScreen();
    fireEvent.click(await screen.findByRole('button', { name: OPEN_SESSION_LABEL }));
    expect(
      await screen.findByText(label('catalog_detail_code', 'catalog_mounting_gate_pending')),
    ).toBeTruthy();
  });
});

describe('reejecución por regresión y vista en vivo (VIG-324)', { timeout: 30_000 }, () => {
  const WAIT = { timeout: 10_000 };
  const RERUN_LABEL = text('scr06.rerun.submit');

  it('abre la reejecución con el cuerpo exacto y retoma la sesión de las filas afectadas', async () => {
    const calls: CapturedRequest[] = [];
    let opened = false;
    server.use(
      http.get(mswPath('/zones/:zone_id/walk-tests/current'), () =>
        HttpResponse.json(
          currentWalkTest(opened ? session({ kind: 'regression_rerun', rows: [row()] }) : null),
        ),
      ),
      captureHandler({
        method: 'post',
        path: '/zones/{zone_id}/walk-tests/regression-rerun',
        calls,
        response: () => {
          opened = true;
          return HttpResponse.json(session({ kind: 'regression_rerun', rows: [row()] }), {
            status: 201,
          });
        },
      }),
    );
    openScreen();
    fireEvent.click(await screen.findByRole('button', { name: RERUN_LABEL }, WAIT));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    }, WAIT);
    expect(calls[0]?.url).toBe(`/zones/${ZONE_ID}/walk-tests/regression-rerun`);
    expect(calls[0]?.body).toEqual({ passes_per_cell: 3 });
    expect(
      await screen.findByText(
        label('walk_test_kind', 'regression_rerun'),
        { selector: '[data-part="session_kind"]' },
        WAIT,
      ),
    ).toBeTruthy();
  });

  it('el rechazo del servidor se ancla al panel de la reejecución', async () => {
    server.use(
      currentWalkTestHandler(currentWalkTest(null)),
      http.post(mswPath('/zones/:zone_id/walk-tests/regression-rerun'), () =>
        apiError('conflict', 409, { detailCode: 'catalog_walk_test_in_progress' }),
      ),
    );
    openScreen();
    fireEvent.click(await screen.findByRole('button', { name: RERUN_LABEL }, WAIT));
    const panel = screen.getByRole('region', { name: text('scr06.rerun.title') });
    await waitFor(() => {
      expect(panel.querySelector('[role="alert"]')?.getAttribute('data-code')).toBe('conflict');
    }, WAIT);
    expect(
      within(panel).getByText(label('catalog_detail_code', 'catalog_walk_test_in_progress')),
    ).toBeTruthy();
  });

  it('con la sesión en curso ofrece la vista en vivo sin pedir ningún token al montar', async () => {
    const tokens: CapturedRequest[] = [];
    server.use(
      currentWalkTestHandler(currentWalkTest(session())),
      captureHandler({
        method: 'post',
        path: '/zones/{zone_id}/live-view-token',
        calls: tokens,
        response: () => HttpResponse.json({}, { status: 500 }),
      }),
    );
    openScreen();
    expect(
      await screen.findByRole('region', { name: text('live_view.region_label') }, WAIT),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: text('live_view.open') })).toBeTruthy();
    expect(tokens).toHaveLength(0);
  });
});

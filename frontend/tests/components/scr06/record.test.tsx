/**
 * Acta de comisionamiento cerrada (IF-13 paso 7; H-53; TASK-435).
 *
 * - Lectura auditada: no se pide al montar; solo con «consultar el acta».
 * - Muestra la matriz, las tasas por cámara, la latencia por tramo (las sumas orientativas), las
 *   horas por tipo de paso y el total **de la zona**; ningún total por responsable aparece en el
 *   árbol aunque el servidor lo mandara.
 * - El documento se abre por navegación, en la misma origen y con `noopener noreferrer`.
 */
import { fireEvent, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { RecordScreen } from '@/pages/scr06/record/RecordScreen';
import { recordDocumentHref } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { mswPath } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  CAMERA_A,
  RECORD_ID,
  RESPONSIBLE,
  captureHandler,
  commissioningRecord,
  renderScreen,
  resetTransport,
  type CapturedRequest,
  type TestSession,
} from './support';

const WAIT = { timeout: 10_000 };
/** Tope de cada prueba: por encima de las esperas de `WAIT` (lección de VIG-300). */
const TEST_TIMEOUT = 30_000;
const REQUEST_LABEL = text('scr06.record.request');
const CORRELATION_ID = '0190a1b2-0000-7000-8000-0000000000fe';

let mounted: TestSession | null = null;

beforeEach(() => {
  mounted = null;
});

afterEach(() => {
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

function openScreen(recordId = RECORD_ID): TestSession {
  const rendered = renderScreen((clock) => <RecordScreen recordId={recordId} clock={clock} />);
  mounted = rendered;
  return rendered;
}

function recordHandler(calls: CapturedRequest[], body: Record<string, unknown>) {
  return captureHandler({
    method: 'get',
    path: '/commissioning-records/{record_id}',
    calls,
    response: () => HttpResponse.json(body),
  });
}

async function requestRecord(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: REQUEST_LABEL }, WAIT));
  return screen.findByRole('article', { name: text('scr06.record.title') }, WAIT);
}

describe('lectura auditada', { timeout: TEST_TIMEOUT }, () => {
  it('no pide el acta al montar; la pide una vez con «consultar el acta»', async () => {
    const calls: CapturedRequest[] = [];
    server.use(recordHandler(calls, { ...commissioningRecord() }));
    openScreen();
    await screen.findByRole('button', { name: REQUEST_LABEL }, WAIT);
    expect(calls).toHaveLength(0);
    await requestRecord();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(`/commissioning-records/${RECORD_ID}`);
  });

  it('un fallo del servidor muestra el error con su correlation_id', async () => {
    server.use(
      http.get(mswPath('/commissioning-records/:record_id'), () =>
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
    fireEvent.click(await screen.findByRole('button', { name: REQUEST_LABEL }, WAIT));
    const code = await screen.findByText(CORRELATION_ID, {}, WAIT);
    expect(code.tagName).toBe('CODE');
  });
});

describe('contenido del acta', { timeout: TEST_TIMEOUT }, () => {
  it('pinta la matriz, las tasas por cámara, la latencia y el documento a demanda', async () => {
    server.use(recordHandler([], { ...commissioningRecord() }));
    openScreen();
    const record = await requestRecord();
    expect(record.querySelectorAll('[data-part="matrix_result"]')).toHaveLength(2);
    expect(
      within(record).getByText('0,125', { selector: '[data-part="false_alarm_rate"]' }),
    ).toBeTruthy();
    expect(
      within(record).getByText('0,2', { selector: '[data-part="false_alarm_threshold"]' }),
    ).toBeTruthy();
    // Tasas por cámara: una medida y otra «no medido».
    const cameras = record.querySelectorAll('[data-part="camera_rate"]');
    expect(cameras).toHaveLength(2);
    expect(within(cameras[0] as HTMLElement).getByText('14,5')).toBeTruthy();
    expect(
      within(cameras[1] as HTMLElement).getAllByText(text('scr06.record.not_measured')),
    ).toHaveLength(1);
    // Latencia: cada tramo con su reloj; los no medidos, marcados.
    const node = record.querySelector('[data-tranche="node_tranche"]');
    expect(node?.querySelector('[data-part="p95_ms"]')?.textContent).toBe('180 ms');
    expect(node?.querySelector('[data-part="measured_by"]')?.textContent).toBe(
      text('scr06.record.measured_by.installer'),
    );
    expect(
      record.querySelector('[data-tranche="exposure_tranche"] [data-part="tranche_not_measured"]'),
    ).not.toBeNull();
    // Oclusión por cámara con la etiqueta de su verificación.
    const occlusion = record.querySelector(
      '[data-part="occlusion_summary"] [data-verification="verified"]',
    );
    expect(occlusion?.textContent).toBe(
      `${text('scr06.record.camera')}: ${CAMERA_A} · ${label('occlusion_verification', 'verified')}`,
    );
    // Documento por navegación: misma origen, pestaña nueva sin referencia.
    const document_ = within(record).getByRole('link', { name: text('scr06.record.document') });
    expect(document_.getAttribute('href')).toBe(`/commissioning-records/${RECORD_ID}/document`);
    expect(document_.getAttribute('rel')).toBe('noopener noreferrer');
    expect(document_.getAttribute('target')).toBe('_blank');
  });

  it.each(['acta-sin-uuid', '../../auth/logout', `${RECORD_ID}/../x`, '', `${RECORD_ID}?a=1`])(
    'la dirección del documento solo sale de un UUID (%s → ninguna)',
    (recordId) => {
      expect(recordDocumentHref(recordId)).toBeNull();
    },
  );
});

describe('horas por zona, nunca por responsable (H-53)', { timeout: TEST_TIMEOUT }, () => {
  it('muestra el desglose por tipo de paso y el total de la zona, sin responsable', async () => {
    // El servidor no manda responsables en el acta; si los mandara (campos ajenos), no se pintan.
    server.use(
      recordHandler([], {
        ...commissioningRecord(),
        steps_summary: [
          {
            step_kind: 'walk_test_passes',
            duration_ms: 3_600_000,
            responsible_user_id: RESPONSIBLE,
          },
          { step_kind: 'framing', duration_ms: 1_800_000 },
        ],
        hours_by_responsible: [{ responsible_user_id: RESPONSIBLE, duration_ms: 5_400_000 }],
      }),
    );
    openScreen();
    const record = await requestRecord();
    const byKind = record.querySelectorAll('[data-part="hours_by_step_kind"] li');
    expect([...byKind].map((item) => item.firstElementChild?.textContent)).toEqual([
      label('step_kind', 'walk_test_passes'),
      label('step_kind', 'framing'),
    ]);
    expect(
      [...byKind].map(
        (item) => item.querySelector('[data-part="step_kind_duration"]')?.textContent,
      ),
    ).toEqual(['1 h 00 min 00 s', '0 h 30 min 00 s']);
    expect(record.querySelector('[data-part="total_duration"]')?.textContent).toBe(
      `${text('scr06.hours.total')}: 1 h 30 min 00 s`,
    );
    // Ningún responsable ni total por responsable en el árbol: ni su texto, ni su rótulo, ni un
    // atributo con su identificador.
    expect(screen.queryAllByText(RESPONSIBLE, { exact: true })).toHaveLength(0);
    expect(screen.queryAllByText(text('scr06.steps.responsible'))).toHaveLength(0);
    const attributes = [...document.querySelectorAll('*')].flatMap((element) =>
      [...element.attributes].map((attribute) => attribute.value),
    );
    expect(attributes.filter((value) => value === RESPONSIBLE)).toEqual([]);
  });

  it('axe no encuentra violaciones serias en el acta', async () => {
    server.use(
      recordHandler([], {
        ...commissioningRecord({
          false_alarm_acceptance: {
            reason_es: 'Reflejos de la ventana corregidos en el encuadre.',
            accepted_by: RESPONSIBLE,
            accepted_at: '2026-10-24T10:59:00.000Z',
          },
        }),
      }),
    );
    const { result } = openScreen();
    await requestRecord();
    const results = await axe(result.container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });
});

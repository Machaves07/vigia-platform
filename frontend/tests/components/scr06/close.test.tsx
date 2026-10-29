/**
 * Cierre del acta de comisionamiento (IF-13 paso 6; D-2; TASK-435).
 *
 * - `beacon_latency_ms_p95` se valida antes de enviar: 10 001 (y −1) no salen; 0 y 10 000 sí, con
 *   el valor exacto en el cuerpo.
 * - El cuerpo es el del contrato: la firma de UI-16 y los cofirmantes, la aceptación de falsas
 *   alarmas solo si se escribe y las líneas base con la zona de la sesión.
 * - Cada rechazo de cierre del servidor se ancla al requisito o al campo que falló.
 * - El acta devuelta se muestra en el acto con su página.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { closeAnchor } from '@/pages/scr06/close/CloseRequirements';
import { CloseScreen, type CloseSigner } from '@/pages/scr06/close/CloseScreen';
import { behaviourFor } from '@/ports/errors/map';
import { PortError } from '@/ports/shared';
import { fill } from '@/pages/scr06/common/format';
import { text } from '@/texts';
import { label, labelInline } from '@/texts/labels';

import { apiError, mswPath } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  CAMERA_A,
  COSIGNER_ID,
  RECORD_ID,
  SESSION_ID,
  SIGNER_ID,
  ZONE_ID,
  captureHandler,
  commissioningRecord,
  currentWalkTest,
  currentWalkTestHandler,
  renderScreen,
  resetTransport,
  session,
  type CapturedRequest,
  type TestSession,
} from './support';

const WAIT = { timeout: 10_000 };
/** Tope de cada prueba: por encima de las esperas de `WAIT` (lección de VIG-300). */
const TEST_TIMEOUT = 30_000;
const REQUIRED = `(${text('components_forms.reason.required')})`;
const BEACON_LABEL = `${text('scr06.close.measurements.beacon')} ${REQUIRED}`;
const SIGN_LABEL = text('components_forms.signature.sign');
const FALSE_ALARM_LABEL = text('scr06.close.false_alarm.reason');

const SIGNER: CloseSigner = {
  user_id: SIGNER_ID,
  display_name: 'Instalación Norte',
  professional_license: 'LIC-SINTETICA-0001',
};

let mounted: TestSession | null = null;

beforeEach(() => {
  mounted = null;
});

afterEach(() => {
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

function openScreen(signer: CloseSigner | null = SIGNER): TestSession {
  const rendered = renderScreen((clock) => (
    <MemoryRouter>
      <CloseScreen zoneId={ZONE_ID} clock={clock} signer={signer} />
    </MemoryRouter>
  ));
  mounted = rendered;
  return rendered;
}

function closeHandler(
  calls: CapturedRequest[],
  response = () => HttpResponse.json(commissioningRecord()),
) {
  return captureHandler({
    method: 'post',
    path: '/walk-tests/{session_id}/close',
    calls,
    response,
  });
}

async function typeBeacon(value: string): Promise<void> {
  fireEvent.change(await screen.findByRole('spinbutton', { name: BEACON_LABEL }, WAIT), {
    target: { value },
  });
}

function beaconError(): string | null {
  const input = screen.getByRole('spinbutton', { name: BEACON_LABEL });
  return (
    input.parentElement
      ?.querySelector('[data-part="local_error"]')
      ?.getAttribute('data-text-key') ?? null
  );
}

describe(
  'installer_measurements: bordes de beacon_latency_ms_p95 (D-2)',
  { timeout: TEST_TIMEOUT },
  () => {
    beforeEach(() => {
      server.use(currentWalkTestHandler(currentWalkTest(session())));
    });

    it.each([
      ['10001', 'validation.out_of_range'],
      ['-1', 'validation.out_of_range'],
      ['', 'validation.required'],
    ])('«%s» no se envía y se marca con %s', async (value, key) => {
      const calls: CapturedRequest[] = [];
      server.use(closeHandler(calls));
      openScreen();
      await typeBeacon(value);
      fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
      await waitFor(() => {
        expect(beaconError()).toBe(key);
      }, WAIT);
      expect(calls).toHaveLength(0);
    });

    it.each([
      ['0', 0],
      ['10000', 10_000],
    ])('«%s» se envía con el valor exacto', async (value, expected) => {
      const calls: CapturedRequest[] = [];
      server.use(closeHandler(calls));
      openScreen();
      await typeBeacon(value);
      fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
      await waitFor(() => {
        expect(calls).toHaveLength(1);
      }, WAIT);
      expect(calls[0]?.url).toBe(`/walk-tests/${SESSION_ID}/close`);
      expect(calls[0]?.body).toEqual({
        signatures: [{ user_id: SIGNER_ID }],
        installer_measurements: { beacon_latency_ms_p95: expected },
      });
    });
  },
);

describe('cuerpo del cierre', { timeout: TEST_TIMEOUT }, () => {
  beforeEach(() => {
    server.use(currentWalkTestHandler(currentWalkTest(session())));
  });

  it('VIG-366: el texto que se firma nombra la sesión en minúscula, a mitad de frase', async () => {
    openScreen();
    const signed = await screen.findByText(
      (_, element) => element?.getAttribute('data-part') === 'text_to_sign',
      {},
      WAIT,
    );
    const kind = labelInline('walk_test_kind', 'initial');
    // La mayúscula inicial de `label()` es para el valor suelto, no para el medio de la frase.
    expect(kind).not.toBe(label('walk_test_kind', 'initial'));
    expect(signed.textContent).toBe(fill(text('scr06.close.sign_text'), { kind, version: '12' }));
  });

  it('lleva cofirmantes, aceptación de falsas alarmas y líneas base con la zona de la sesión', async () => {
    const calls: CapturedRequest[] = [];
    server.use(closeHandler(calls));
    openScreen();
    await typeBeacon('180');
    fireEvent.click(screen.getByRole('button', { name: text('scr06.close.cosigners.add') }));
    fireEvent.change(
      screen.getByRole('textbox', { name: `Identificador del cofirmante 1 ${REQUIRED}` }),
      { target: { value: COSIGNER_ID } },
    );
    fireEvent.change(screen.getByRole('textbox', { name: FALSE_ALARM_LABEL }), {
      target: { value: 'Las falsas alarmas se deben a reflejos ya corregidos en el encuadre.' },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr06.close.baselines.add') }));
    fireEvent.change(
      screen.getByRole('textbox', {
        name: `Cámara de la línea base 1 (identificador) ${REQUIRED}`,
      }),
      { target: { value: CAMERA_A } },
    );
    fireEvent.change(
      screen.getByRole('textbox', {
        name: `Captura de la línea base 1 (ISO 8601 con zona) ${REQUIRED}`,
      }),
      { target: { value: '2026-10-24T09:30:00.000Z' } },
    );
    fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    }, WAIT);
    expect(calls[0]?.body).toEqual({
      signatures: [{ user_id: SIGNER_ID }, { user_id: COSIGNER_ID }],
      false_alarm_acceptance: {
        reason_es: 'Las falsas alarmas se deben a reflejos ya corregidos en el encuadre.',
      },
      installer_measurements: {
        beacon_latency_ms_p95: 180,
        baselines: [
          { camera_id: CAMERA_A, zone_id: ZONE_ID, captured_at: '2026-10-24T09:30:00.000Z' },
        ],
      },
    });
  });

  it('un cofirmante con identificador mal formado no se envía y se marca en su campo', async () => {
    const calls: CapturedRequest[] = [];
    server.use(closeHandler(calls));
    openScreen();
    await typeBeacon('180');
    fireEvent.click(screen.getByRole('button', { name: text('scr06.close.cosigners.add') }));
    const cosigner = screen.getByRole('textbox', {
      name: `Identificador del cofirmante 1 ${REQUIRED}`,
    });
    fireEvent.change(cosigner, { target: { value: 'no-es-un-uuid' } });
    fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
    await waitFor(() => {
      expect(cosigner.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    expect(calls).toHaveLength(0);
  });

  it('al cerrarse muestra el acta y el enlace a su página', async () => {
    server.use(closeHandler([]));
    openScreen();
    await typeBeacon('180');
    fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
    const link = await screen.findByRole('link', { name: text('scr06.close.open_record') }, WAIT);
    expect(link.getAttribute('href')).toBe(`/zones/${ZONE_ID}/commissioning/records/${RECORD_ID}`);
    expect(document.querySelector('[data-part="commissioning_record"]')).not.toBeNull();
    expect(
      screen.getByRole('link', { name: text('scr06.record.document') }).getAttribute('href'),
    ).toBe(`/commissioning-records/${RECORD_ID}/document`);
  });

  it('sin persona de la sesión no hay bloque de firma', async () => {
    openScreen(null);
    expect(await screen.findByText(text('scr06.close.no_signer'), {}, WAIT)).toBeTruthy();
    expect(screen.queryByRole('button', { name: SIGN_LABEL })).toBeNull();
  });
});

describe('rechazos del cierre anclados', { timeout: TEST_TIMEOUT }, () => {
  beforeEach(() => {
    server.use(currentWalkTestHandler(currentWalkTest(session())));
  });

  it.each([
    ['catalog_false_negative_present', 'matrix'],
    ['catalog_matrix_incomplete', 'matrix'],
    ['catalog_passes_below_minimum', 'matrix'],
    ['catalog_steps_still_open', 'steps'],
    ['catalog_walk_test_incomplete', 'steps'],
    ['catalog_redundancy_not_verified', 'occlusion'],
    ['catalog_blur_not_verified', 'blur'],
    ['catalog_latency_not_measured', 'latency'],
  ])('%s se ancla al requisito «%s»', async (detailCode, requirement) => {
    server.use(
      http.post(mswPath('/walk-tests/:session_id/close'), () =>
        apiError('conflict', 409, { detailCode }),
      ),
    );
    openScreen();
    await typeBeacon('180');
    fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
    await waitFor(() => {
      expect(document.querySelectorAll('[data-requirement] [role="alert"]')).toHaveLength(1);
    }, WAIT);
    const alert = document.querySelector(`[data-requirement="${requirement}"] [role="alert"]`);
    expect(alert?.getAttribute('data-code')).toBe('conflict');
  });

  it.each([
    ['catalog_false_negative_present', 'matrix'],
    ['toString', null],
    ['constructor', null],
    ['catalog_desconocido', null],
  ])('closeAnchor(%s) → %s', (detailCode, anchor) => {
    const error = { code: 'conflict', status: 409, detail_code: detailCode };
    const failure = new PortError({ kind: 'http', error, behaviour: behaviourFor(error) });
    expect(closeAnchor(failure)).toBe(anchor);
  });

  it('un detail_code con nombre del prototipo («toString») va a la pila, sin ancla', async () => {
    server.use(
      http.post(mswPath('/walk-tests/:session_id/close'), () =>
        apiError('conflict', 409, { detailCode: 'toString' }),
      ),
    );
    openScreen();
    await typeBeacon('180');
    fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
    await waitFor(() => {
      expect(document.querySelectorAll('[data-code="conflict"]')).toHaveLength(1);
    }, WAIT);
    expect(document.querySelectorAll('[data-requirement] [role="alert"]')).toHaveLength(0);
  });

  it('catalog_false_alarm_rate_above_threshold se ancla al motivo de la aceptación', async () => {
    server.use(
      http.post(mswPath('/walk-tests/:session_id/close'), () =>
        apiError('conflict', 409, { detailCode: 'catalog_false_alarm_rate_above_threshold' }),
      ),
    );
    openScreen();
    await typeBeacon('180');
    fireEvent.click(screen.getByRole('button', { name: SIGN_LABEL }));
    const reason = screen.getByRole('textbox', { name: FALSE_ALARM_LABEL });
    await waitFor(() => {
      expect(reason.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    expect(reason.parentElement?.querySelector('[role="alert"]')?.getAttribute('data-code')).toBe(
      'conflict',
    );
    expect(document.querySelectorAll('[data-requirement] [role="alert"]')).toHaveLength(0);
  });
});

describe('estados comunes', { timeout: TEST_TIMEOUT }, () => {
  it('axe no encuentra violaciones serias en el cierre', async () => {
    server.use(currentWalkTestHandler(currentWalkTest(session())));
    const { result } = openScreen();
    await screen.findByRole('spinbutton', { name: BEACON_LABEL }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('scr06.close.cosigners.add') }));
    fireEvent.click(screen.getByRole('button', { name: text('scr06.close.baselines.add') }));
    const results = await axe(result.container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });

  it('sin sesión muestra el estado de la consola', async () => {
    server.use(currentWalkTestHandler(currentWalkTest(null)));
    openScreen();
    expect(await screen.findByText(text('scr06.matrix.empty'), {}, WAIT)).toBeTruthy();
  });
});

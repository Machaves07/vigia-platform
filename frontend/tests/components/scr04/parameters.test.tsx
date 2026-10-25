import { act, fireEvent, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr04/parameters';
import {
  bodyIssues,
  parametersPort,
  screenLabel,
  parameterReasonLimits,
  vocabularyReasonLimits,
} from '@/ports/scr04Parameters';
import { readFileSync } from 'node:fs';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { assertNoPersonData } from '../../support/viewProperties';
import views from '../../views/scr04_parameters';
import { CAMERA, catalog, errorResponse, REASON, renderPage, WAIT, ZONE } from './support';

async function ready() {
  const view = await renderPage(<Page />);
  await screen.findByRole('form', { name: text('scr04.parameters.title') }, WAIT);
  return view;
}
function change(key: string, value: string, role = 'spinbutton') {
  fireEvent.change(screen.getByRole(role, { name: text(`scr04.parameters.${key}`) }), {
    target: { value },
  });
}
function reason() {
  fireEvent.change(
    screen.getByRole('textbox', {
      name: `${text('scr04.parameters.reason')} (${text('components_forms.reason.required')})`,
    }),
    { target: { value: REASON } },
  );
}
function submit() {
  fireEvent.submit(screen.getByRole('form', { name: text('scr04.parameters.title') }));
}

describe('SCR-04 parámetros', { timeout: 60_000 }, () => {
  it.each(Object.entries(views))(
    'RV-07 %s no filtra campos inyectados',
    (_name, view) => {
      assertNoPersonData(view);
    },
    180_000,
  );
  it('contrasta los límites de motivo con las dos políticas exportadas del backend', () => {
    const corpus = JSON.parse(readFileSync('../backend/openapi/u05-fixtures.json', 'utf8')) as {
      free_text: { routes: { route: string; min_length: number; max_length: number }[] };
    };
    for (const [route, limits] of [
      ['catalog_version_published/reason_es', parameterReasonLimits],
      ['vocabulary_list_changed/reason', vocabularyReasonLimits],
    ] as const) {
      const policy = corpus.free_text.routes.find((entry) => entry.route === route);
      expect(policy).toBeDefined();
      expect(limits).toEqual({ minLength: policy?.min_length, maxLength: policy?.max_length });
    }
  });

  it.each([0, 1, 8, 9])('espejo de cámaras: límite %s', (count) => {
    const cameras = Array.from({ length: count }, () => ({
      camera_id: CAMERA,
      code: 'CAM-SINT',
      role_in_zone: 'primary',
      declared_min_fps: 12,
      stream_reference: 'camara-sintetica',
    }));
    expect(
      bodyIssues('cameras_zones__zone_id__cameras_put', { cameras, reason_es: REASON }).length ===
        0,
    ).toBe(count >= 1 && count <= 8);
  });

  it.each([0, 32, 33])('espejo de señales: límite %s', (count) => {
    const signals = Array.from({ length: count }, () => ({}));
    expect(
      bodyIssues('signals_zones__zone_id__signals_put', { signals, reason_es: REASON }).length ===
        0,
    ).toBe(count <= 32);
  });

  it('envía cámaras con función, fps y referencia por el puerto propio', async () => {
    await ready();
    change('parameter', 'cameras', 'combobox');
    const calls: unknown[] = [];
    server.use(
      http.put('http://localhost/zones/:zone/cameras', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(catalog(2));
      }),
    );
    change('camera_id', CAMERA, 'textbox');
    change('code', 'CAM-SINT', 'textbox');
    change('declared_min_fps', '12');
    change('stream_reference', 'camara-sintetica', 'textbox');
    reason();
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls).toEqual([
      {
        cameras: [
          {
            camera_id: CAMERA,
            code: 'CAM-SINT',
            role_in_zone: 'primary',
            declared_min_fps: 12,
            stream_reference: 'camara-sintetica',
          },
        ],
        reason_es: REASON,
      },
    ]);
  });

  it('configura una señal con campos estructurados y permite retirar todas las señales', async () => {
    await ready();
    change('parameter', 'signals', 'combobox');
    const calls: unknown[] = [];
    server.use(
      http.put('http://localhost/zones/:zone/signals', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(catalog(2));
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: text('scr04.parameters.add_signal') }));
    change('signal_id', CAMERA, 'textbox');
    change('code', 'SIG-SINT', 'textbox');
    change('description_es', 'Señal sintética de guarda', 'textbox');
    change('reader', 'lector-sintetico', 'textbox');
    change('channel', '2');
    reason();
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls).toEqual([
      {
        signals: [
          {
            signal_id: CAMERA,
            code: 'SIG-SINT',
            description_es: 'Señal sintética de guarda',
            role: 'auxiliary',
            asserted_level: 'high',
            source: { reader: 'lector-sintetico', channel: 2 },
          },
        ],
        reason_es: REASON,
      },
    ]);
    fireEvent.click(screen.getByRole('button', { name: text('scr04.parameters.remove_signal') }));
    reason();
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls[1]).toEqual({ signals: [], reason_es: REASON });
  });

  it('envía ambas ventanas con motivo sin mezclar los parámetros de otros formularios', async () => {
    await ready();
    change('parameter', 'windows', 'combobox');
    const calls: unknown[] = [];
    server.use(
      http.put('http://localhost/zones/:zone/windows', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(catalog(2));
      }),
    );
    reason();
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls).toEqual([
      {
        clip_window: { pre_seconds: 10, post_seconds: 10 },
        episode: { grouping_window_ms: 3000, max_segment_ms: 900000 },
        reason_es: REASON,
      },
    ]);
  });
  it('declara las once operaciones fixed y sin ocultar escrituras por reglas locales', () => {
    expect(parametersPort.operations).toHaveLength(11);
    expect(
      parametersPort.operations.every(
        (operation) => operation.status === 'fixed' && operation.api_operation_id !== null,
      ),
    ).toBe(true);
  });
  it.each([
    [0.8, 0.8],
    [0.9, 0.8],
    [0, 0.8],
    [0.5, 1.1],
  ])('no envía umbrales inválidos %s / %s', async (review, publication) => {
    await ready();
    let calls = 0;
    server.use(
      http.put('http://localhost/zones/:zone/thresholds', () => {
        calls += 1;
        return HttpResponse.json(catalog(2));
      }),
    );
    reason();
    change('review', String(review));
    change('publication', String(publication));
    submit();
    await screen.findByRole('alert', {}, WAIT);
    expect(calls).toBe(0);
  });
  it('envía 0 < revisión < publicación = 1 con motivo, recibe versión y refresca regresión', async () => {
    const view = await ready();
    const calls: unknown[] = [];
    server.use(
      http.put('http://localhost/zones/:zone/thresholds', async ({ request, params }) => {
        expect(params.zone).toBe(ZONE);
        calls.push(await request.json());
        return HttpResponse.json(catalog(2));
      }),
    );
    reason();
    change('review', '0.5');
    change('publication', '1');
    submit();
    await screen.findByText(`${text('scr04.parameters.version')} 2`, {}, WAIT);
    expect(calls).toEqual([{ review: 0.5, publication: 1, reason_es: REASON }]);
    expect(
      view.container.querySelector('[data-role-in-use]')?.getAttribute('data-role-in-use'),
    ).toBe('administrator');
    expect(
      (await axe(view.container)).violations.filter(
        (item) => item.impact === 'serious' || item.impact === 'critical',
      ),
    ).toEqual([]);
  });
  it('prevalece el aviso del servidor y conserva el borrador sin reintentar', async () => {
    const { clock } = await ready();
    let writes = 0;
    server.use(
      http.put('http://localhost/zones/:zone/thresholds', () => {
        writes += 1;
        return errorResponse('catalog_invalid_thresholds');
      }),
    );
    reason();
    submit();
    await screen.findByText('Mensaje sintético del servidor.', {}, WAIT);
    expect(
      screen.getByRole<HTMLInputElement>('spinbutton', { name: text('scr04.parameters.review') })
        .value,
    ).toBe('0.4');
    await act(async () => {
      clock.advance(60_000);
      await Promise.resolve();
    });
    expect(writes).toBe(1);
  });
  it('ancla catalog_unsatisfiable_coverage sin repetir la decisión de cobertura del servidor', async () => {
    await ready();
    change('parameter', 'minimum_coverage', 'combobox');
    reason();
    server.use(
      http.put('http://localhost/zones/:zone/minimum-coverage', () =>
        errorResponse('catalog_unsatisfiable_coverage'),
      ),
    );
    submit();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-code')).toBe('invalid_request');
    expect(alert.textContent).not.toBe('');
    expect(
      screen.getByText(screenLabel('catalog_detail_code', 'catalog_unsatisfiable_coverage')),
    ).toBeTruthy();
  });
  it('un motivo no válido impide el envío de cualquier parámetro', async () => {
    await ready();
    let writes = 0;
    server.use(
      http.put('http://localhost/zones/:zone/thresholds', () => {
        writes += 1;
        return HttpResponse.json(catalog(2));
      }),
    );
    submit();
    await screen.findByText(text('validation.required'), {}, WAIT);
    expect(writes).toBe(0);
  });
});

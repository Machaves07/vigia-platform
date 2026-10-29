import { act, fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr04/admissions';
import { zoneName } from '@/components/TimestampWithOffset/format';
import { AdmissionView } from '@/pages/scr04/admissions/AdmissionView';
import { CRITERIA, isRecordedRejection, screenLabel } from '@/ports/scr04Parameters';
import { PortError } from '@/ports/base';
import { behaviourFor } from '@/ports/errors/map';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { admission, errorResponse, INSTANT, PLANT, renderPage, WAIT } from './support';

async function answer(negative: string | null) {
  for (const criterion of CRITERIA) {
    fireEvent.click(
      screen.getByRole('radio', {
        name: text(`scr04.admissions.${criterion === negative ? 'no' : 'yes'}`),
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.next') }));
  }
  fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.submit') }));
}

describe('SCR-04 admisión', { timeout: 60_000 }, () => {
  it.each([
    ['America/Bogota', '02/03/2026, 07:00:00'],
    ['Europe/Madrid', '02/03/2026, 13:00:00'],
  ])('presenta la evaluación en %s con la zona una vez, en la cabecera', (timeZone, local) => {
    const { container } = render(<AdmissionView data={admission()} timeZone={timeZone} />);
    const instant = container.querySelector('time');
    expect(instant?.getAttribute('datetime')).toBe(INSTANT);
    expect(instant?.textContent).toBe(local);
    // VIG-366: la zona va en el nombre de la columna, no en cada celda.
    expect(screen.getByText(`Fecha de evaluación (${zoneName(timeZone) ?? ''})`)).toBeTruthy();
    expect(container.querySelector('[data-part="zone"]')).toBeNull();
  });

  it('pliega el autor y el registro en «Datos técnicos» (VIG-366)', () => {
    const { container } = render(<AdmissionView data={admission()} />);
    const technical = container.querySelector('details[data-part="technical"]');
    expect(technical).not.toBeNull();
    expect(technical?.hasAttribute('open')).toBe(false);
    const values = [...(technical?.querySelectorAll('dd code') ?? [])].map((n) => n.textContent);
    expect(values).toEqual([admission().evaluated_by, admission().ledger_record_id]);
  });

  it.each([
    [400, 'invalid_request', 'catalog_admission_rejected', true],
    [422, 'invalid_request', 'catalog_admission_rejected', false],
    [400, 'conflict', 'catalog_admission_rejected', false],
    [400, 'invalid_request', 'catalog_predicate_invalid', false],
  ] as const)('A-56 exige status=%s code=%s detail=%s', (status, code, detail_code, expected) => {
    const error = { status, code, detail_code };
    expect(
      isRecordedRejection(new PortError({ kind: 'http', error, behaviour: behaviourFor(error) })),
    ).toBe(expected);
  });
  it('lee failed_criterion del historial cuando la API actual omite el campo del 400', async () => {
    await renderPage(<Page />, `/plants/${PLANT}/admissions`);
    let registered = false;
    server.use(
      http.post('http://localhost/plants/:plant/admissions', () => {
        registered = true;
        return errorResponse('catalog_admission_rejected');
      }),
      http.get('http://localhost/plants/:plant/admissions', () =>
        HttpResponse.json({
          admissions: registered
            ? [
                {
                  ...admission(),
                  answers: { standard: false, remedy: true, subject: true },
                  result: 'rejected',
                  failed_criterion: 'remedy',
                },
              ]
            : [],
          next_after: null,
        }),
      ),
    );
    await answer('standard');
    await screen.findByRole('heading', { name: text('scr04.admissions.recorded') }, WAIT);
    expect(
      screen.getByText(
        `${text('scr04.admissions.failed_criterion')}: ${screenLabel('admission_criterion', 'remedy')}`,
      ),
    ).toBeTruthy();
    expect(screen.queryAllByRole('alert')).toHaveLength(0);
  });

  it('un 400 genérico sigue siendo un error y conserva el resumen', async () => {
    await renderPage(<Page />, `/plants/${PLANT}/admissions`);
    server.use(
      http.post('http://localhost/plants/:plant/admissions', () =>
        errorResponse('catalog_predicate_invalid'),
      ),
    );
    await answer('standard');
    await screen.findByText(
      screenLabel('catalog_detail_code', 'catalog_predicate_invalid'),
      {},
      WAIT,
    );
    expect(screen.queryByRole('heading', { name: text('scr04.admissions.recorded') })).toBeNull();
  });
  it.each(CRITERIA)(
    'envía el no de %s y presenta el 400 A-56 como evaluación registrada, no error',
    async (criterion) => {
      const view = await renderPage(<Page />, `/plants/${PLANT}/admissions`);
      const calls: unknown[] = [];
      let historyReads = 0;
      server.use(
        http.post('http://localhost/plants/:plant/admissions', async ({ request, params }) => {
          expect(params.plant).toBe(PLANT);
          calls.push(await request.json());
          return errorResponse('catalog_admission_rejected', { failed_criterion: criterion });
        }),
        http.get('http://localhost/plants/:plant/admissions', () => {
          historyReads += 1;
          return HttpResponse.json({
            admissions:
              calls.length === 0
                ? []
                : [{ ...admission(), result: 'rejected', failed_criterion: criterion }],
            next_after: null,
          });
        }),
      );
      await answer(criterion);
      await screen.findByRole('heading', { name: text('scr04.admissions.recorded') }, WAIT);
      expect(calls).toEqual([
        {
          family: 'coexistence',
          answers: {
            standard: criterion !== 'standard',
            remedy: criterion !== 'remedy',
            subject: criterion !== 'subject',
          },
        },
      ]);
      const status = view.container.querySelector('[data-failed-criterion]');
      expect(status?.getAttribute('data-failed-criterion')).toBe(criterion);
      expect(status?.textContent).toBe(
        `${text('scr04.admissions.failed_criterion')}: ${screenLabel('admission_criterion', criterion)}`,
      );
      expect(screen.queryAllByRole('alert')).toHaveLength(0);
      await screen.findByRole('cell', { name: screenLabel('admission_result', 'rejected') }, WAIT);
      expect(historyReads).toBeGreaterThan(0);
    },
  );

  it('registra una evaluación admitida con el rol del servidor y accesibilidad seria limpia', async () => {
    const view = await renderPage(<Page />, `/plants/${PLANT}/admissions`);
    expect(
      (await axe(view.container)).violations.filter(
        (item) => item.impact === 'serious' || item.impact === 'critical',
      ),
    ).toEqual([]);
    server.use(
      http.post('http://localhost/plants/:plant/admissions', () =>
        HttpResponse.json(admission(), { status: 201 }),
      ),
    );
    await answer(null);
    await screen.findByRole('heading', { name: text('scr04.admissions.recorded') }, WAIT);
    expect(
      view.container.querySelector('[data-role-in-use]')?.getAttribute('data-role-in-use'),
    ).toBe('administrator');
  });

  it.each([{ failed_criterion: 'unknown' }, {}])(
    'no inventa un criterio ni anuncia admisión si el rechazo no permite leerlo: %j',
    async (extra) => {
      await renderPage(<Page />, `/plants/${PLANT}/admissions`);
      server.use(
        http.post('http://localhost/plants/:plant/admissions', () =>
          errorResponse('catalog_admission_rejected', extra),
        ),
      );
      await answer('standard');
      await screen.findByRole('heading', { name: text('scr04.admissions.recorded') }, WAIT);
      expect(screen.getByText(text('scr04.admissions.criterion_unavailable'))).toBeTruthy();
      expect(screen.queryByText(screenLabel('admission_result', 'admitted'))).toBeNull();
    },
  );

  it('reenvía el cursor opaco del historial sin fabricar páginas ni operaciones sobre registros', async () => {
    await renderPage(<Page />, `/plants/${PLANT}/admissions`);
    await screen.findByText(text('components_structure.keyset.empty'), {}, WAIT);
    const queries: string[] = [];
    server.use(
      http.get('http://localhost/plants/:plant/admissions', ({ request }) => {
        const url = new URL(request.url);
        queries.push(url.searchParams.get('after') ?? '');
        return HttpResponse.json({
          admissions: queries.length === 1 ? [admission()] : [],
          next_after: queries.length === 1 ? 'cursor-sintetico' : null,
        });
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await screen.findByRole('cell', { name: screenLabel('admission_result', 'admitted') }, WAIT);
    const history = screen.getByRole('table', { name: text('scr04.admissions.history') });
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.keyset.load_more') }),
    );
    await waitFor(() => {
      expect(queries).toEqual(['', 'cursor-sintetico']);
    }, WAIT);
    expect(within(history).queryAllByRole('button')).toHaveLength(0);
    expect(queries).toEqual(['', 'cursor-sintetico']);
  });
});

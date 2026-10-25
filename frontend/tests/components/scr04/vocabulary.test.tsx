import { fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr04/vocabulary';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { errorResponse, INSTANT, RECORD, REASON, renderPage, WAIT } from './support';

function fill(key: string, value: string) {
  const label = text(`scr04.vocabulary.${key}`);
  fireEvent.change(
    screen.getByRole('textbox', {
      name: key === 'reason' ? `${label} (${text('components_forms.reason.required')})` : label,
    }),
    { target: { value } },
  );
}
function submit() {
  fireEvent.submit(screen.getByRole('form', { name: text('scr04.vocabulary.save') }));
}

describe('SCR-04 vocabulario', { timeout: 60_000 }, () => {
  it('un cambio sin motivo no se envía; con motivo crea un registro inmutable y axe limpio', async () => {
    const view = await renderPage(<Page />, '/admin/vocabulary');
    await screen.findByRole('table', { name: text('scr04.vocabulary.title') }, WAIT);
    const calls: unknown[] = [];
    server.use(
      http.post('http://localhost/loop/vocabulary/changes', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json(
          {
            change_id: RECORD,
            record_id: RECORD,
            list_version: 2,
            changed_at: INSTANT,
            added: [],
            removed: [],
            declared_names_added: [],
            declared_names_removed: [],
          },
          { status: 201 },
        );
      }),
    );
    fill('value', 'expresión sintética');
    fill('suggestion', 'Descripción observable');
    submit();
    await screen.findByText(text('validation.required'), {}, WAIT);
    expect(calls).toHaveLength(0);
    fill('reason', REASON);
    submit();
    await screen.findByText(RECORD, {}, WAIT);
    expect(calls).toEqual([
      {
        added: [
          {
            category: 'absence_claim',
            form: 'lemma',
            value: 'expresión sintética',
            suggestion_es: 'Descripción observable',
          },
        ],
        removed: [],
        declared_names_added: [],
        declared_names_removed: [],
        reason: REASON,
      },
    ]);
    const history = screen.getByRole('region', { name: text('scr04.vocabulary.history') });
    expect(within(history).queryAllByRole('button')).toHaveLength(0);
    expect(within(history).queryAllByRole('textbox')).toHaveLength(0);
    expect(
      (await axe(view.container)).violations.filter(
        (item) => item.impact === 'serious' || item.impact === 'critical',
      ),
    ).toEqual([]);
  });

  it('no oculta retirar derivados: envía la solicitud y muestra el rechazo del servidor', async () => {
    await renderPage(<Page />, '/admin/vocabulary');
    fireEvent.change(screen.getByRole('combobox', { name: text('scr04.vocabulary.action') }), {
      target: { value: 'remove' },
    });
    await screen.findByRole('option', { name: 'término sintético' }, WAIT);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr04.vocabulary.term_id') }), {
      target: { value: RECORD },
    });
    fill('reason', REASON);
    const calls: unknown[] = [];
    server.use(
      http.post('http://localhost/loop/vocabulary/changes', async ({ request }) => {
        calls.push(await request.json());
        return errorResponse('loop_derived_term_irreducible');
      }),
    );
    submit();
    await screen.findByText('Mensaje sintético del servidor.', {}, WAIT);
    expect(calls).toEqual([
      {
        added: [],
        removed: [RECORD],
        declared_names_added: [],
        declared_names_removed: [],
        reason: REASON,
      },
    ]);
    expect(
      screen.getByRole<HTMLSelectElement>('combobox', { name: text('scr04.vocabulary.term_id') })
        .value,
    ).toBe(RECORD);
  });
});

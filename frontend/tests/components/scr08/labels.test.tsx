/**
 * SCR-08, pestaña de etiquetas (`labels.read`; `GET /labels`): a demanda, por zona, familia y
 * resultado, sin imágenes ni texto libre. Las aserciones comparan claves del catálogo, etiquetas,
 * parámetros de la consulta y atributos; nunca subcadenas de texto con datos (VIG-194).
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { settle } from '../../support/queryCache';
import { EVIDENCE, fixture, labelRow, WAIT, ZONE, ZONE_NAME } from './detail_support';

const PATH = '/findings/labels';

function fill(from: string, to: string) {
  fireEvent.change(screen.getByLabelText(text('scr08.labels.from')), { target: { value: from } });
  fireEvent.change(screen.getByLabelText(text('scr08.labels.to')), { target: { value: to } });
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: text('scr08.labels.submit') }));
}

describe('SCR-08 etiquetas', { timeout: 60_000 }, () => {
  it('no pide nada al abrir la pestaña ni con el paso del tiempo', async () => {
    const env = await fixture({ path: PATH });
    env.view();
    await screen.findByRole('form', { name: text('scr08.labels.filters') }, WAIT);
    for (let minute = 0; minute < 5; minute += 1) {
      await act(async () => {
        env.clock.advance(60_000);
        await settle();
      });
    }
    expect(env.calls.labels).toHaveLength(0);
  });

  it('consulta a demanda con el periodo y los filtros de zona, familia y resultado', async () => {
    const env = await fixture({ path: PATH });
    env.view();
    await screen.findByRole('option', { name: ZONE_NAME }, WAIT);
    fill('2026-01-01', '2026-01-31');
    fireEvent.change(screen.getByLabelText(text('scr08.labels.zone')), { target: { value: ZONE } });
    fireEvent.change(screen.getByLabelText(text('scr08.labels.family')), {
      target: { value: 'guard_bypass' },
    });
    fireEvent.change(screen.getByLabelText(text('scr08.labels.reason_category')), {
      target: { value: 'reflection_or_glare' },
    });
    submit();
    await screen.findByRole('table', {}, WAIT);
    expect(env.calls.labels).toHaveLength(1);
    const query = env.calls.labels[0]?.searchParams;
    expect(query?.get('from')).toBe('2026-01-01T00:00:00.000Z');
    expect(query?.get('to')).toBe('2026-02-01T00:00:00.000Z');
    expect(query?.get('zone_id')).toBe(ZONE);
    expect(query?.get('family')).toBe('guard_bypass');
    expect(query?.get('reason_category')).toBe('reflection_or_glare');
    expect(query?.has('after_label_id')).toBe(false);
  });

  it('cada fila muestra zona, familia, salida, resultado, fecha y rol; nunca evidencias ni autor', async () => {
    const env = await fixture({ path: PATH });
    const view = env.view();
    await screen.findByRole('option', { name: ZONE_NAME }, WAIT);
    fill('2026-01-01', '2026-01-02');
    submit();
    const table = await screen.findByRole('table', {}, WAIT);
    const cells = within(table)
      .getAllByRole('cell')
      .map((cell) => cell.textContent);
    expect(cells.slice(0, 4)).toEqual([
      ZONE_NAME,
      label('predicate_family', 'guard_bypass'),
      label('classification_outcome', 'false_positive'),
      label('reason_category_false_positive', 'reflection_or_glare'),
    ]);
    expect(cells[5]).toBe(label('role', 'coordinator_sst'));
    expect(within(table).getAllByRole('cell')[4]?.querySelector('time')).not.toBeNull();
    expect(view.container.querySelector('img, video')).toBeNull();
    expect(screen.queryByText(EVIDENCE, { exact: false })).toBeNull();
    expect(screen.queryByText(labelRow(1).labeled_by_user_id ?? '', { exact: false })).toBeNull();
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it('un periodo inválido no consulta y lo dice', async () => {
    const env = await fixture({ path: PATH });
    env.view();
    await screen.findByRole('form', { name: text('scr08.labels.filters') }, WAIT);
    fill('2026-01-31', '2026-01-01');
    submit();
    expect(screen.getByRole('alert').textContent).toBe(text('scr08.labels.invalid_period'));
    fill('', '2026-01-01');
    submit();
    expect(screen.getByRole('alert').textContent).toBe(text('scr08.labels.invalid_period'));
    await act(async () => {
      await settle();
    });
    expect(env.calls.labels).toHaveLength(0);
  });

  it('«cargar más» sigue el cursor opaco del servidor', async () => {
    const env = await fixture({ path: PATH });
    env.responses.labels = (url) =>
      url.searchParams.has('after_label_id')
        ? HttpResponse.json({ items: [labelRow(2)], next_cursor: null })
        : HttpResponse.json({
            items: [labelRow(1)],
            next_cursor: { labeled_at: '2026-01-01T12:00:00.000Z', label_id: labelRow(1).label_id },
          });
    env.view();
    await screen.findByRole('form', { name: text('scr08.labels.filters') }, WAIT);
    fill('2026-01-01', '2026-01-02');
    submit();
    await screen.findByRole('table', {}, WAIT);
    fireEvent.click(
      await screen.findByRole(
        'button',
        { name: text('components_structure.keyset.load_more') },
        WAIT,
      ),
    );
    await waitFor(() => {
      expect(env.calls.labels).toHaveLength(2);
    }, WAIT);
    const next = env.calls.labels[1]?.searchParams;
    expect(next?.get('after_label_id')).toBe(labelRow(1).label_id);
    expect(next?.get('after_labeled_at')).toBe('2026-01-01T12:00:00.000Z');
    expect(next?.get('from')).toBe('2026-01-01T00:00:00.000Z');
    await waitFor(() => {
      expect(screen.getAllByRole('row')).toHaveLength(3);
    }, WAIT);
  });
});

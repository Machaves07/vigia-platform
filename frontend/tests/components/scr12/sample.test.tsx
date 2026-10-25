/** H-39: el único POST de auditoría es SUP-U05-26; no hay clasificación en el puerto. */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { monthlyPort } from '@/ports/scr12Monthly';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { apiError, ITEM, setup, WAIT } from './support';

describe('SCR-12 muestra auditada', { timeout: 60_000 }, () => {
  it.each(['consistent_criterion', 'doubtful_criterion', 'criterion_error'])(
    'resultado %s solo envía SUP-U05-26 sin tocar clasificación',
    async (outcome) => {
      const { calls } = await setup();
      await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
      expect(calls.reads).not.toContain('sample');
      fireEvent.click(screen.getByRole('button', { name: text('scr12.load_sample') }));
      const form = await screen.findByRole('form', { name: text('scr12.submit_result') }, WAIT);
      fireEvent.change(within(form).getByRole('combobox', { name: text('scr12.outcome') }), {
        target: { value: outcome },
      });
      fireEvent.change(within(form).getByRole('textbox', { name: text('scr12.comment') }), {
        target: { value: 'Comentario sintético sobre el criterio aplicado a la zona.' },
      });
      fireEvent.click(within(form).getByRole('button'));
      await within(form).findByText(text('scr12.saved'), {}, WAIT);
      expect(calls.writes).toEqual([
        {
          method: 'POST',
          path: `/accreditation/sample-items/${ITEM}/result`,
          body: { outcome, comment: 'Comentario sintético sobre el criterio aplicado a la zona.' },
        },
      ]);
      const section = screen.getByRole('region', { name: text('scr12.sample') });
      expect(
        within(section).getByText(
          `${text('scr12.classification')}: ${label('classification_outcome', 'confirmed')}`,
        ).textContent,
      ).toBe(`${text('scr12.classification')}: ${label('classification_outcome', 'confirmed')}`);
      expect(
        monthlyPort.operations.filter((op) => op.kind === 'write').map((op) => op.api_operation_id),
      ).toEqual([
        'record_sample_result_accreditation_sample_items__sample_item_id__result_post',
        'record_commitments_accreditation_monthly_packages__package_id__commitments_post',
        'update_commitment_accreditation_commitments__commitment_id__patch',
      ]);
    },
  );

  it('el mando consulta la muestra sin acción sobre el registro, salvo compromisos', async () => {
    const { calls } = await setup({ permissions: ['review_package.read', 'commitments.write'] });
    await screen.findByRole('form', { name: text('scr12.create') }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('scr12.load_sample') }));
    await screen.findByText(text('scr12.pending'), { selector: 'p' }, WAIT);
    expect(screen.queryByRole('form', { name: text('scr12.submit_result') })).toBeNull();
    expect(calls.writes).toEqual([]);
  });

  it('409 ya revisado se presenta anclado sin reintentar ni eliminar la clasificación', async () => {
    const { calls } = await setup({
      resultResponse: () => apiError('accreditation_sample_already_reviewed'),
    });
    await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('scr12.load_sample') }));
    const form = await screen.findByRole('form', { name: text('scr12.submit_result') }, WAIT);
    fireEvent.change(within(form).getByRole('combobox'), { target: { value: 'criterion_error' } });
    fireEvent.click(within(form).getByRole('button'));
    await within(form).findByRole('alert', {}, WAIT);
    await waitFor(() => {
      expect(calls.writes).toHaveLength(1);
    }, WAIT);
    expect(within(form).getByRole('combobox')).toHaveProperty('value', 'criterion_error');
  });
});

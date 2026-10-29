/** SUP-U05-27 a 29, BR-APP-24/33/43: texto, ejecutor y plazo; actualizar añade registro. */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { text } from '@/texts';
import loopLabels from '../../../../backend/resources/labels.loop.es.json';
import { changeDate } from '../../support/dateFields';
import { USER_A } from '../../support/session';
import { apiError, ITEM, PACKAGE, setup, WAIT } from './support';

async function fill() {
  const form = await screen.findByRole('form', { name: text('scr12.create') }, WAIT);
  fireEvent.change(within(form).getByRole('textbox', { name: text('scr12.text') }), {
    target: { value: 'Instalar un resguardo sintético sobre la zona.' },
  });
  fireEvent.change(within(form).getByRole('textbox', { name: text('scr12.responsible') }), {
    target: { value: USER_A },
  });
  changeDate(within(form).getByLabelText(text('scr12.due')), '2026-03-20');
  return form;
}

describe('SCR-12 compromisos', { timeout: 60_000 }, () => {
  it('registra únicamente texto, responsable y plazo y consulta el resultado', async () => {
    const { calls } = await setup();
    const form = await fill();
    expect(
      Array.from(form.querySelectorAll('[name]')).map((node) => node.getAttribute('name')),
    ).toEqual(['text', 'responsible_user_id', 'due_date']);
    fireEvent.click(within(form).getByRole('button', { name: text('scr12.create') }));
    const update = await screen.findByRole('form', { name: text('scr12.update') }, WAIT);
    expect(calls.writes).toEqual([
      {
        method: 'POST',
        path: `/accreditation/monthly-packages/${PACKAGE}/commitments`,
        body: {
          commitments: [
            {
              text: 'Instalar un resguardo sintético sobre la zona.',
              responsible_user_id: USER_A,
              due_date: '2026-03-20',
            },
          ],
        },
      },
    ]);
    fireEvent.change(within(update).getByRole('combobox', { name: text('scr12.state') }), {
      target: { value: 'done' },
    });
    fireEvent.change(within(update).getByRole('textbox', { name: text('scr12.note') }), {
      target: { value: 'Resguardo instalado sobre la zona.' },
    });
    fireEvent.click(within(update).getByRole('button', { name: text('scr12.update') }));
    await waitFor(() => {
      expect(calls.writes).toHaveLength(2);
    }, WAIT);
    expect(calls.writes[1]).toEqual({
      method: 'PATCH',
      path: `/accreditation/commitments/${ITEM}`,
      body: { state: 'done', note: 'Resguardo instalado sobre la zona.' },
    });
  });

  it.each([
    'accreditation_monthly_package_not_available',
    'accreditation_commitment_out_of_scope',
    'loop_person_attribution_detected',
  ])('presenta %s anclado y conserva el borrador sin inferirlo antes', async (detail) => {
    const { calls } = await setup({
      createResponse: () =>
        apiError(
          detail,
          detail === 'accreditation_monthly_package_not_available' ? 409 : 400,
          detail === 'accreditation_monthly_package_not_available' ? 'conflict' : 'invalid_request',
        ),
    });
    const form = await fill();
    fireEvent.click(within(form).getByRole('button', { name: text('scr12.create') }));
    await within(form).findByRole('alert', {}, WAIT);
    expect(within(form).getByRole('alert').dataset.code).toBe(
      detail === 'accreditation_monthly_package_not_available' ? 'conflict' : 'invalid_request',
    );
    expect(within(form).getByRole('textbox', { name: text('scr12.text') })).toHaveProperty(
      'value',
      'Instalar un resguardo sintético sobre la zona.',
    );
    expect(calls.writes).toHaveLength(1);
    expect(within(form).getByRole('button', { name: text('scr12.create') })).toHaveProperty(
      'disabled',
      false,
    );
    expect(
      within(form).getByText(
        detail === 'loop_person_attribution_detected'
          ? loopLabels.loop_detail_code.loop_person_attribution_detected
          : loopLabels.accreditation_detail_code[
              detail as keyof typeof loopLabels.accreditation_detail_code
            ],
      ),
    ).toBeTruthy();
  });

  it('sin commitments.write lee la lista pero no ofrece escrituras', async () => {
    await setup({ permissions: ['review_package.read'] });
    await screen.findByText(text('scr12.commitments_empty'), {}, WAIT);
    expect(screen.queryByRole('form', { name: text('scr12.create') })).toBeNull();
  });

  it('campos vacíos y texto demasiado corto no envían escrituras; límites declarados', async () => {
    const { calls } = await setup();
    const form = await screen.findByRole('form', { name: text('scr12.create') }, WAIT);
    const value = within(form).getByRole('textbox', { name: text('scr12.text') });
    expect(value).toHaveProperty('minLength', 20);
    expect(value).toHaveProperty('maxLength', 1000);
    fireEvent.click(within(form).getByRole('button'));
    expect(calls.writes).toEqual([]);
  });
});

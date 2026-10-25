import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { text } from '@/texts';
import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  DESCRIPTION,
  FINDING,
  USER,
  WAIT,
  detail,
  load,
  reasonName,
  renderClosure,
} from './closure_support';

describe('SCR-09 reapertura', { timeout: 40_000 }, () => {
  it('reabrir exige motivo y acción nueva completa; el cierre anterior sigue visible después de actualizar', async () => {
    const { container } = await renderClosure('reopening');
    const user = await load();
    const bodies: unknown[] = [];
    let reopened = false;
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}`, () =>
        HttpResponse.json({ ...detail, lifecycle_state: reopened ? 'action_assigned' : 'closed' }),
      ),
      http.post(`http://localhost/loop/findings/${FINDING}/reopening`, async ({ request }) => {
        bodies.push(await request.json());
        reopened = true;
        return HttpResponse.json(
          { ...(exampleFor('reopen_loop_findings__finding_state_id__reopening_post') as object) },
          { status: 201 },
        );
      }),
    );
    const form = screen.getByRole('form', { name: text('scr09.reopening.title') });
    await user.click(within(form).getByRole('button', { name: text('scr09.reopening.submit') }));
    expect(bodies).toEqual([]);
    fireEvent.change(
      within(form).getByRole('textbox', { name: reasonName('scr09.reopening.reason') }),
      { target: { value: DESCRIPTION } },
    );
    fireEvent.change(
      within(form).getByRole('textbox', { name: reasonName('scr09.reopening.action_description') }),
      { target: { value: DESCRIPTION } },
    );
    await user.selectOptions(
      within(form).getByLabelText(text('scr09.reopening.action_type')),
      'engineering',
    );
    await user.click(within(form).getByRole('button', { name: text('scr09.reopening.submit') }));
    await within(form).findByText(text('scr09.closure.saved'), {}, WAIT);
    expect(bodies).toEqual([
      {
        reason_text: DESCRIPTION,
        action_type: 'engineering',
        action_description: DESCRIPTION,
        expected_outcome: null,
        assignee_user_id: USER,
        deadline_days: null,
      },
    ]);
    // También después de refrescar la proyección: el historial viene del expediente, no del estado.
    await user.click(screen.getByRole('button', { name: text('scr09.closure.load') }));
    await waitFor(() => {
      expect(container.querySelectorAll('[data-record-type="closure"]')).toHaveLength(1);
    }, WAIT);
    const history = screen.getByRole('region', { name: text('scr09.closure.history') });
    expect(within(history).getByText(DESCRIPTION)).not.toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('409 conserva el motivo y la acción, sin reintentar la escritura', async () => {
    let writes = 0;
    await renderClosure('reopening');
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/reopening`, () => {
        writes++;
        return apiError('conflict', 409, { detailCode: 'loop_transition_not_allowed' });
      }),
    );
    fireEvent.change(screen.getByRole('textbox', { name: reasonName('scr09.reopening.reason') }), {
      target: { value: DESCRIPTION },
    });
    fireEvent.change(
      screen.getByRole('textbox', { name: reasonName('scr09.reopening.action_description') }),
      { target: { value: DESCRIPTION } },
    );
    await user.selectOptions(screen.getByLabelText(text('scr09.reopening.action_type')), 'process');
    await user.click(screen.getByRole('button', { name: text('scr09.reopening.submit') }));
    await waitFor(() => {
      expect(document.querySelector('[data-code="conflict"]')).not.toBeNull();
    }, WAIT);
    expect(
      screen.getByRole<HTMLTextAreaElement>('textbox', {
        name: reasonName('scr09.reopening.reason'),
      }).value,
    ).toBe(DESCRIPTION);
    expect(writes).toBe(1);
  });
});

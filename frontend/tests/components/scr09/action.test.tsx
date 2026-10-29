import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
import { detail, FINDING, INSTANT, renderScreen, USER, WAIT } from './classify_support';

const DESCRIPTION = 'Instalar un enclavamiento en la puerta del acceso lateral de la prensa';
const EXPECTED = 'La puerta abierta detiene el ciclo de la prensa';
const DESCRIPTION_NAME = `${text('scr09.loop.action_description')} (${text('components_forms.reason.required')})`;
const DEADLINE_NAME = `${text('scr09.loop.deadline_days')} (${text('scr09.loop.deadline_default')}: 15)`;
const validate = openApiValidators(
  (readAppYaml().components as { schemas: Record<string, unknown> }).schemas,
).component('CorrectiveActionBody');

async function openForm() {
  server.use(
    http.get(`http://localhost/loop/findings/${FINDING}`, () =>
      HttpResponse.json(detail({ lifecycle_state: 'open' })),
    ),
  );
  const rendered = await renderScreen('loop');
  const user = userEvent.setup();
  // El lazo abre cargado (VIG-365); «Comprometer una acción» del panel abre el formulario.
  await user.click(
    await screen.findByRole('button', { name: text('next_step.access.action') }, WAIT),
  );
  const form = await screen.findByRole('form', { name: text('scr09.loop.action_form') }, WAIT);
  // El plazo por defecto llega de la configuración vigente de la planta.
  await waitFor(() => {
    expect(screen.getByRole<HTMLInputElement>('spinbutton', { name: DEADLINE_NAME }).value).toBe(
      '15',
    );
  }, WAIT);
  return { ...rendered, user, form };
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(
    screen.getByRole('combobox', { name: text('scr09.loop.action_type') }),
    'engineering',
  );
  await user.type(screen.getByRole('textbox', { name: DESCRIPTION_NAME }), DESCRIPTION);
  await user.type(
    screen.getByRole('textbox', { name: text('scr09.loop.expected_outcome') }),
    EXPECTED,
  );
  await user.click(screen.getByRole('button', { name: text('scr09.loop.submit') }));
}

describe('SCR-09 acción sobre la zona', { timeout: 40_000 }, () => {
  it('sin campo de persona observada; el cuerpo valida con el esquema generado y sin campos extra', async () => {
    const bodies: unknown[] = [];
    const { user, form, container } = await openForm();
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/actions`, async ({ request }) => {
        const body: unknown = await request.json();
        bodies.push(body);
        if (!validate(body)) return apiError('invalid_request', 400);
        return HttpResponse.json(
          {
            finding_state_id: FINDING,
            action_id: '0190a1b2-7777-7000-8000-000000000001',
            record_id: '0190a1b2-7777-7000-8000-000000000002',
            action_type: 'engineering',
            assignee_user_id: USER,
            deadline_days: 15,
            due_at: INSTANT,
            supersedes_action_id: null,
            lifecycle_state: 'action_assigned',
            assigned_at: INSTANT,
            time_aggregated: false,
          },
          { status: 201 },
        );
      }),
    );
    const controls = [...form.querySelectorAll('input, select, textarea')].map((control) =>
      control.getAttribute('name'),
    );
    expect(controls).toEqual([
      'action_type',
      'action_description',
      'expected_outcome',
      'assignee_user_id',
      'deadline_days',
    ]);
    // El responsable es el usuario de la plataforma que se compromete, nunca una persona observada.
    expect(
      [...form.querySelectorAll('select[name="assignee_user_id"] option')].map((o) =>
        o.getAttribute('value'),
      ),
    ).toEqual([USER]);
    await fill(user);
    await waitFor(() => {
      expect(container.querySelector('[data-part="action_saved"]')).not.toBeNull();
    }, WAIT);
    expect(bodies).toEqual([
      {
        action_type: 'engineering',
        action_description: DESCRIPTION,
        expected_outcome: EXPECTED,
        assignee_user_id: USER,
        deadline_days: 15,
      },
    ]);
    expect(validate(bodies[0])).toBe(true);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('un 409 loop_transition_not_allowed se ancla al formulario y conserva lo escrito', async () => {
    const { user, form } = await openForm();
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/actions`, () =>
        apiError('conflict', 409, { detailCode: 'loop_transition_not_allowed' }),
      ),
    );
    await fill(user);
    await waitFor(() => {
      expect(form.getAttribute('aria-describedby')?.split(' ')).toHaveLength(2);
    }, WAIT);
    const anchored = document.getElementById(
      form.getAttribute('aria-describedby')?.split(' ')[1] ?? '',
    );
    expect(form.contains(anchored)).toBe(true);
    expect(anchored?.getAttribute('role')).toBe('alert');
    expect(anchored?.getAttribute('data-code')).toBe('conflict');
    expect(anchored?.querySelector('[data-part="message"]')?.textContent).toBe(
      label('loop_detail_code', 'loop_transition_not_allowed'),
    );
    const type = screen.getByRole<HTMLSelectElement>('combobox', {
      name: text('scr09.loop.action_type'),
    });
    expect(type.value).toBe('engineering');
    const description = screen.getByRole<HTMLTextAreaElement>('textbox', {
      name: DESCRIPTION_NAME,
    });
    expect(description.value).toBe(DESCRIPTION);
    const expected = screen.getByRole<HTMLTextAreaElement>('textbox', {
      name: text('scr09.loop.expected_outcome'),
    });
    expect(expected.value).toBe(EXPECTED);
    expect(form.querySelector('[data-part="action_saved"]')).toBeNull();
  });

  it('un plazo fuera del intervalo se ancla al campo del plazo', async () => {
    const { user } = await openForm();
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/actions`, () =>
        apiError('invalid_request', 400, { detailCode: 'loop_deadline_out_of_range' }),
      ),
    );
    await fill(user);
    const deadline = screen.getByRole('spinbutton', { name: DEADLINE_NAME });
    await waitFor(() => {
      expect(deadline.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    const anchored = document.getElementById(deadline.getAttribute('aria-describedby') ?? '');
    expect(anchored?.querySelector('[data-part="message"]')?.textContent).toBe(
      label('loop_detail_code', 'loop_deadline_out_of_range'),
    );
    expect((deadline as HTMLInputElement).value).toBe('15');
  });
});

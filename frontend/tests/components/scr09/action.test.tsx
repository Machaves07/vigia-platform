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
import type { Assignee } from '@/ports/scr09Closure';
import {
  assigneesHandler,
  detail,
  FINDING,
  INSTANT,
  renderScreen,
  SESSION_ASSIGNEE,
  USER,
  WAIT,
} from './classify_support';

const DESCRIPTION = 'Instalar un enclavamiento en la puerta del acceso lateral de la prensa';
const EXPECTED = 'La puerta abierta detiene el ciclo de la prensa';
const DESCRIPTION_NAME = `${text('scr09.loop.action_description')} (${text('components_forms.reason.required')})`;
const EXPECTED_NAME = `${text('scr09.loop.expected_outcome')} (${text('components_forms.reason.optional')})`;
const DEADLINE_NAME = `${text('scr09.loop.deadline_days')} (${text('scr09.loop.deadline_default')}: 15)`;
const validate = openApiValidators(
  (readAppYaml().components as { schemas: Record<string, unknown> }).schemas,
).component('CorrectiveActionBody');

/** `setup` corre tras pintar la pantalla y antes de abrir el formulario (sus manejadores mandan). */
async function openForm(setup: () => void = () => undefined) {
  server.use(
    http.get(`http://localhost/loop/findings/${FINDING}`, () =>
      HttpResponse.json(detail({ lifecycle_state: 'open' })),
    ),
  );
  const rendered = await renderScreen('loop');
  setup();
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
  await user.type(screen.getByRole('textbox', { name: EXPECTED_NAME }), EXPECTED);
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
      name: EXPECTED_NAME,
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

// --- TASK-583 (O-3, A-126): el responsable real de la acción ----------------------------------

const ALVARO_PERSON: Assignee = {
  user_id: '0190a1b2-4444-7000-8000-000000000004',
  display_name: 'Álvaro Prueba',
  roles: ['line_manager'],
};
const PEOPLE: readonly Assignee[] = [
  {
    user_id: '0190a1b2-4444-7000-8000-000000000002',
    display_name: 'Elena Prueba',
    roles: ['line_manager'],
  },
  SESSION_ASSIGNEE,
  {
    user_id: '0190a1b2-4444-7000-8000-000000000003',
    display_name: 'Bruno Prueba',
    roles: ['plant_manager'],
  },
  ALVARO_PERSON,
  {
    user_id: '0190a1b2-4444-7000-8000-000000000005',
    display_name: 'Celia Prueba',
    roles: ['line_manager', 'plant_manager'],
  },
  {
    user_id: '0190a1b2-4444-7000-8000-000000000006',
    display_name: 'Dario Prueba',
    roles: ['coordinator_sst'],
  },
];
const ELENA = '0190a1b2-4444-7000-8000-000000000002';
const ALVARO = ALVARO_PERSON.user_id;
const ASSIGNEE_NAME = text('scr09.loop.assignee');
const SEARCH_NAME = text('scr09.assignee.search');

function caption(person: Assignee): string {
  const own = person.user_id === USER ? ` (${text('scr09.assignee.you')})` : '';
  return `${person.display_name}${own} · ${person.roles.map((role) => label('role', role)).join(', ')}`;
}

function options(): { value: string | null; text: string | null }[] {
  return [...screen.getByRole<HTMLSelectElement>('combobox', { name: ASSIGNEE_NAME }).options].map(
    (option) => ({ value: option.getAttribute('value'), text: option.textContent }),
  );
}

function created(bodies: unknown[]) {
  return http.post(`http://localhost/loop/findings/${FINDING}/actions`, async ({ request }) => {
    const body: unknown = await request.json();
    bodies.push(body);
    if (!validate(body)) return apiError('invalid_request', 400);
    return HttpResponse.json(
      {
        finding_state_id: FINDING,
        action_id: '0190a1b2-7777-7000-8000-000000000001',
        record_id: '0190a1b2-7777-7000-8000-000000000002',
        action_type: 'engineering',
        assignee_user_id: (body as { assignee_user_id: string }).assignee_user_id,
        deadline_days: 15,
        due_at: INSTANT,
        supersedes_action_id: null,
        lifecycle_state: 'action_assigned',
        assigned_at: INSTANT,
        time_aggregated: false,
      },
      { status: 201 },
    );
  });
}

/** Abre el formulario con `people` como lista de responsables (en páginas de `pageSize`). */
async function openWith(people: readonly Assignee[], pageSize = 200) {
  const listed = assigneesHandler(people, pageSize);
  const opened = await openForm(() => {
    server.use(listed.handler);
  });
  await waitFor(() => {
    expect(options()).toHaveLength(people.length);
  }, WAIT);
  return { ...opened, requests: listed.requests };
}

describe('SCR-09 responsable de la acción (TASK-583)', { timeout: 40_000 }, () => {
  it('lista a las personas que assign acepta, con sus roles y la persona en sesión primero, y crea la acción con la elegida', async () => {
    const bodies: unknown[] = [];
    const { user, container, requests } = await openWith(PEOPLE);
    server.use(created(bodies));
    // La sesión primero (la sugerencia) y después por nombre, con los roles etiquetados.
    const byName = [
      'Álvaro Prueba',
      'Bruno Prueba',
      'Celia Prueba',
      'Dario Prueba',
      'Elena Prueba',
    ];
    expect(options()).toEqual(
      [SESSION_ASSIGNEE, ...byName.map((name) => PEOPLE.find((p) => p.display_name === name))].map(
        (person) => ({
          value: person?.user_id ?? null,
          text: person === undefined ? null : caption(person),
        }),
      ),
    );
    expect(screen.getByRole<HTMLSelectElement>('combobox', { name: ASSIGNEE_NAME }).value).toBe(
      USER,
    );
    // Una sola página de hasta 200, sin parámetro de búsqueda (BR-APP-21).
    expect(requests.map((url) => [...url.searchParams.entries()])).toEqual([[['limit', '200']]]);
    await user.selectOptions(screen.getByRole('combobox', { name: ASSIGNEE_NAME }), ELENA);
    await fill(user);
    await waitFor(() => {
      expect(container.querySelector('[data-part="action_saved"]')).not.toBeNull();
    }, WAIT);
    expect(bodies).toEqual([
      {
        action_type: 'engineering',
        action_description: DESCRIPTION,
        expected_outcome: EXPECTED,
        assignee_user_id: ELENA,
        deadline_days: 15,
      },
    ]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('la búsqueda local filtra por nombre sin tildes ni mayúsculas y no pide nada a la API', async () => {
    const seen: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      seen.push(`${request.method} ${new URL(request.url).pathname}`);
    };
    const bodies: unknown[] = [];
    const { user, requests } = await openWith(PEOPLE);
    server.use(created(bodies));
    server.events.on('request:start', listener);
    try {
      await user.type(screen.getByRole('searchbox', { name: SEARCH_NAME }), 'ALVA');
      expect(options()).toEqual([{ value: ALVARO, text: caption(ALVARO_PERSON) }]);
      expect(screen.getByRole<HTMLSelectElement>('combobox', { name: ASSIGNEE_NAME }).value).toBe(
        ALVARO,
      );
      expect(seen).toEqual([]);
      await user.clear(screen.getByRole('searchbox', { name: SEARCH_NAME }));
      // Sin texto vuelve la lista completa y la elegida no cambia.
      expect(options()).toHaveLength(PEOPLE.length);
      expect(screen.getByRole<HTMLSelectElement>('combobox', { name: ASSIGNEE_NAME }).value).toBe(
        ALVARO,
      );
      await user.type(screen.getByRole('searchbox', { name: SEARCH_NAME }), 'zzz');
      expect(screen.getByRole('status').textContent).toBe(text('scr09.assignee.no_match'));
      // Sin coincidencias se conserva la elegida.
      expect(options().map((option) => option.value)).toEqual([ALVARO]);
      expect(seen).toEqual([]);
    } finally {
      server.events.removeListener('request:start', listener);
    }
    expect(requests).toHaveLength(1);
    await fill(user);
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect((bodies[0] as { assignee_user_id: string }).assignee_user_id).toBe(ALVARO);
  });

  it('con pocas personas no hay búsqueda (pocos campos)', async () => {
    await openWith(PEOPLE.slice(0, 2));
    expect(screen.queryByRole('searchbox', { name: SEARCH_NAME })).toBeNull();
  });

  it('recorre todas las páginas por clave antes de ofrecer la lista', async () => {
    const { requests } = await openWith(PEOPLE, 2);
    expect(requests.map((url) => url.searchParams.get('cursor'))).toEqual([
      null,
      '0000000000000000000002',
      '0000000000000000000004',
    ]);
    expect(new Set(options().map((option) => option.value))).toEqual(
      new Set(PEOPLE.map((person) => person.user_id)),
    );
  });

  it('si la persona en sesión no está en la lista, propone a la primera de la lista', async () => {
    const others = PEOPLE.filter((person) => person.user_id !== USER);
    const bodies: unknown[] = [];
    const { user } = await openWith(others);
    server.use(created(bodies));
    await waitFor(() => {
      expect(screen.getByRole<HTMLSelectElement>('combobox', { name: ASSIGNEE_NAME }).value).toBe(
        ALVARO,
      );
    }, WAIT);
    expect(options().map((option) => option.value)).not.toContain(USER);
    // Lo enviado es lo que se ve: nunca la persona en sesión, que assign rechazaría.
    await fill(user);
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect((bodies[0] as { assignee_user_id: string }).assignee_user_id).toBe(ALVARO);
  });

  it('sin la lista, ofrece a la persona en sesión y lo dice', async () => {
    const bodies: unknown[] = [];
    const { user } = await openForm(() => {
      server.use(
        http.get(`http://localhost/loop/findings/${FINDING}/assignees`, () =>
          apiError('not_found', 404),
        ),
      );
    });
    server.use(created(bodies));
    await screen.findByText(text('scr09.assignee.unavailable'), {}, WAIT);
    expect(options()).toEqual([{ value: USER, text: 'Persona sintética (usted)' }]);
    await fill(user);
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect((bodies[0] as { assignee_user_id: string }).assignee_user_id).toBe(USER);
  });
});

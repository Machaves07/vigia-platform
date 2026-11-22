/**
 * SCR-09 (A-126, TASK-580; BR-APP-42 a 44, 78; A-68, A-73): los textos libres del lazo guían en vez
 * de rechazar, en todos los formularios con texto (clasificación, acción, evidencia, cierre,
 * reapertura, incerrable y su revisión):
 *
 * - el motivo real de cada rechazo del servidor (atribución a persona con su sugerencia,
 *   vocabulario bloqueado con término y sugerencia, 400 sin `detail_code` con el texto de revisión
 *   y el `correlation_id`), anclado al campo (o al formulario, si hay varios textos escritos) y
 *   conservando lo escrito;
 * - la validación local antes de enviar: un Enter final se recorta, un salto intermedio avisa y no
 *   envía hasta «Unir en una línea», y un texto corto no envía.
 *
 * El servidor de MSW simula a mano la política base (un carácter de control o menos de 20
 * caracteres es un 400 sin `detail_code`), sin usar el espejo de la interfaz. Se compara por clave
 * de texto, `data-part` y cuerpos por igualdad; nunca por subcadena (VIG-194). Datos sintéticos.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { LONG_TEXT } from '@/pages/scr09/loop/closure/Evidence';
import { CLASSIFICATION_REASON_LIMITS } from '@/ports/scr09Classify';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { changeDate, changeDateTime } from '../../support/dateFields';
import { detail as findingDetail, history, renderScreen } from './classify_support';
import {
  ACTION,
  DECLARATION,
  DESCRIPTION,
  FINDING,
  ledgerRecord,
  load,
  reasonName,
  renderClosure,
  USER,
  WAIT,
} from './closure_support';

type User = ReturnType<typeof userEvent.setup>;

const TEXT = 'Texto sintético del lazo con longitud suficiente';
const WITH_NAMES = 'Revisión sintética del Turno Alfa en la prensa lateral';
const CONTROL = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u;

/** La política base del servidor, a mano: control o menos de 20 caracteres → 400 sin detalle. */
function basePolicyRejects(value: unknown): boolean {
  return (
    typeof value === 'string' &&
    (CONTROL.test(value) || Array.from(value.normalize('NFC')).length < 20)
  );
}

interface Form {
  readonly name: string;
  readonly url: string;
  readonly operation: string;
  /** El texto principal en el cuerpo. */
  readonly field: string;
  /** Dónde se ancla un rechazo de texto: al campo, o al formulario si hay varios textos. */
  readonly anchor: 'field' | 'form';
  readonly open: () => Promise<User>;
  readonly textbox: () => HTMLTextAreaElement;
  readonly submit: (user: User) => Promise<void>;
}

const required = (key: string) => `${text(key)} (${text('components_forms.reason.required')})`;

const CLASSIFY: Form = {
  name: 'clasificación',
  url: `http://localhost/loop/findings/${FINDING}/classifications`,
  operation: 'classify_loop_findings__finding_state_id__classifications_post',
  field: 'reason_text',
  anchor: 'field',
  async open() {
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}`, () =>
        HttpResponse.json(
          findingDetail({
            lifecycle_state: 'received',
            classifications: [],
            classification_count: 0,
          }),
        ),
      ),
      http.get(`http://localhost/loop/findings/${FINDING}/classifications`, () =>
        HttpResponse.json(history([])),
      ),
    );
    await renderScreen('classify');
    const user = userEvent.setup();
    const form = await screen.findByRole('form', { name: text('scr09.classify.form') }, WAIT);
    await user.click(
      within(form).getByRole('radio', { name: label('classification_outcome', 'confirmed') }),
    );
    await user.selectOptions(
      within(form).getByRole('combobox', { name: text('scr09.classify.reason_category') }),
      'procedure_gap',
    );
    return user;
  },
  textbox: () => screen.getByRole('textbox', { name: required('scr09.classify.reason_text') }),
  submit: (user) => user.click(screen.getByRole('button', { name: text('scr09.classify.submit') })),
};

const ACTION_FORM: Form = {
  name: 'acción',
  url: `http://localhost/loop/findings/${FINDING}/actions`,
  operation: 'assign_loop_findings__finding_state_id__actions_post',
  field: 'action_description',
  anchor: 'field',
  async open() {
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}`, () =>
        HttpResponse.json(findingDetail({ lifecycle_state: 'open' })),
      ),
    );
    await renderScreen('loop');
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', { name: text('next_step.access.action') }, WAIT),
    );
    const form = await screen.findByRole('form', { name: text('scr09.loop.action_form') }, WAIT);
    await waitFor(() => {
      expect(
        within(form).getByRole<HTMLInputElement>('spinbutton', {
          name: `${text('scr09.loop.deadline_days')} (${text('scr09.loop.deadline_default')}: 15)`,
        }).value,
      ).toBe('15');
    }, WAIT);
    await user.selectOptions(
      within(form).getByRole('combobox', { name: text('scr09.loop.action_type') }),
      'engineering',
    );
    return user;
  },
  textbox: () => screen.getByRole('textbox', { name: required('scr09.loop.action_description') }),
  submit: (user) => user.click(screen.getByRole('button', { name: text('scr09.loop.submit') })),
};

const EVIDENCE: Form = {
  name: 'evidencia',
  url: `http://localhost/loop/actions/${ACTION}/evidence`,
  operation: 'add_evidence_loop_actions__action_id__evidence_post',
  field: 'description',
  anchor: 'field',
  async open() {
    await renderClosure();
    const user = await load();
    changeDateTime(screen.getByLabelText(text('scr09.closure.performed_at')), '2026-10-24T10:00');
    await user.type(
      screen.getByLabelText(text('scr09.closure.performed_by_role')),
      'Mantenimiento',
    );
    return user;
  },
  textbox: () => screen.getByRole('textbox', { name: reasonName('scr09.closure.description') }),
  submit: (user) =>
    user.click(screen.getByRole('button', { name: text('scr09.closure.submit_evidence') })),
};

const CLOSE: Form = {
  name: 'cierre',
  url: `http://localhost/loop/findings/${FINDING}/closure`,
  operation: 'close_loop_findings__finding_state_id__closure_post',
  field: 'closure_description',
  anchor: 'field',
  async open() {
    await renderClosure();
    return load();
  },
  textbox: () =>
    screen.getByRole('textbox', { name: reasonName('scr09.closure.sign_description') }),
  submit: (user) =>
    user.click(screen.getByRole('button', { name: text('components_forms.signature.sign') })),
};

const REOPENING: Form = {
  name: 'reapertura',
  url: `http://localhost/loop/findings/${FINDING}/reopening`,
  operation: 'reopen_loop_findings__finding_state_id__reopening_post',
  field: 'reason_text',
  // El motivo y la acción nueva son dos textos escritos: el servidor no dice cuál cayó.
  anchor: 'form',
  async open() {
    await renderClosure('reopening');
    const user = await load();
    fireEvent.change(
      screen.getByRole('textbox', { name: reasonName('scr09.reopening.action_description') }),
      { target: { value: `${DESCRIPTION}\n` } },
    );
    await user.selectOptions(
      screen.getByLabelText(text('scr09.reopening.action_type')),
      'engineering',
    );
    return user;
  },
  textbox: () => screen.getByRole('textbox', { name: reasonName('scr09.reopening.reason') }),
  submit: (user) =>
    user.click(screen.getByRole('button', { name: text('scr09.reopening.submit') })),
};

function reviewRegion(): HTMLElement {
  return screen.getByRole('region', { name: text('scr09.unclosable.review') });
}

const REVIEW: Form = {
  name: 'revisión del incerrable',
  url: `http://localhost/loop/unclosable/${DECLARATION}/reviews`,
  operation: 'review_loop_unclosable__declaration_id__reviews_post',
  field: 'review_note',
  anchor: 'form',
  async open() {
    await renderClosure('unclosable', {
      records: [
        ledgerRecord('unclosable_state_declared', {
          declaration_id: DECLARATION,
          evaluated_alternative: DESCRIPTION,
        }),
      ],
    });
    const user = await load();
    await user.click(screen.getByRole('button', { name: text('scr09.unclosable.review') }));
    await user.selectOptions(
      within(reviewRegion()).getByLabelText(text('scr09.unclosable.declaration')),
      DECLARATION,
    );
    changeDate(
      within(reviewRegion()).getByLabelText(text('scr09.unclosable.review_due_at')),
      '2026-10-25',
    );
    return user;
  },
  textbox: () =>
    within(reviewRegion()).getByRole('textbox', {
      name: reasonName('scr09.unclosable.review_note'),
    }),
  submit: (user) =>
    user.click(
      within(reviewRegion()).getByRole('button', {
        name: text('components_forms.signature.sign'),
      }),
    ),
};

/** Formularios que envían con un botón propio: cubren la validación local. */
const DIRECT = [CLASSIFY, ACTION_FORM, EVIDENCE, CLOSE, REOPENING, REVIEW];

/** Escribe el texto principal y envía (la declaración del incerrable recorre su asistente). */
async function send(form: Form, user: User, value: string): Promise<void> {
  fireEvent.change(form.textbox(), { target: { value } });
  await form.submit(user);
}

/** Servidor del formulario: guarda cada cuerpo; responde lo guionizado o la política base. */
function install(form: Pick<Form, 'url' | 'operation' | 'field'>) {
  const state = {
    bodies: [] as Record<string, unknown>[],
    scripted: null as (() => Response) | null,
  };
  server.use(
    http.post(form.url, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      state.bodies.push(body);
      if (state.scripted !== null) return state.scripted();
      if (basePolicyRejects(body[form.field])) return apiError('invalid_request', 400);
      return HttpResponse.json({ ...(exampleFor(form.operation) as object) }, { status: 201 });
    }),
  );
  return state;
}

/** El aviso que el campo referencia con `aria-describedby` y tiene `role="alert"`. */
function anchoredTo(field: HTMLElement): HTMLElement | null {
  const ids = field.getAttribute('aria-describedby')?.split(' ') ?? [];
  return (
    ids
      .map((id) => document.getElementById(id))
      .find((element) => element?.getAttribute('role') === 'alert') ?? null
  );
}

/** El único aviso del servidor de la pantalla (`invalid_request`). */
function serverAlert(): HTMLElement | null {
  const found = document.querySelectorAll<HTMLElement>(
    '[role="alert"][data-code="invalid_request"]',
  );
  return found.length === 1 ? (found[0] ?? null) : null;
}

async function rejection(form: Form, read: () => HTMLTextAreaElement | null = form.textbox) {
  let alert: HTMLElement | null = null;
  await waitFor(() => {
    alert = form.anchor === 'field' ? anchoredTo(read() ?? document.body) : serverAlert();
    expect(alert).not.toBeNull();
  }, WAIT);
  // La declaración ya no muestra su primer paso: allí el aviso solo puede estar en el formulario.
  if (form.anchor === 'form' && form.name !== 'declaración del incerrable') {
    expect(anchoredTo(form.textbox())).toBeNull();
  }
  return alert as unknown as HTMLElement;
}

// --- Declaración del incerrable: el texto principal está en el primer paso del asistente ------

const DECLARATION_FORM: Form = {
  name: 'declaración del incerrable',
  url: `http://localhost/loop/findings/${FINDING}/unclosable`,
  operation: 'declare_loop_findings__finding_state_id__unclosable_post',
  field: 'evaluated_alternative',
  anchor: 'form',
  async open() {
    await renderClosure('unclosable');
    return load();
  },
  textbox: () =>
    screen.getByRole('textbox', { name: reasonName('scr09.unclosable.evaluated_alternative') }),
  async submit(user) {
    const wizard = screen.getByRole('region', { name: text('scr09.unclosable.title') });
    const next = () =>
      user.click(
        within(wizard).getByRole('button', { name: text('components_structure.wizard.next') }),
      );
    await next();
    fireEvent.change(
      within(wizard).getByRole('textbox', {
        name: reasonName('scr09.unclosable.rejection_reason'),
      }),
      { target: { value: `${DESCRIPTION}\n` } },
    );
    await next();
    await next();
    changeDate(within(wizard).getByLabelText(text('scr09.unclosable.decision_date')), '2026-10-24');
    await next();
    fireEvent.change(
      within(wizard).getByRole('textbox', {
        name: reasonName('scr09.unclosable.compensatory_measure'),
      }),
      { target: { value: `${DESCRIPTION}\n` } },
    );
    await next();
    changeDate(within(wizard).getByLabelText(text('scr09.unclosable.review_due_at')), '2026-10-25');
    await user.click(
      within(wizard).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    await next();
    await user.click(
      within(wizard).getByRole('button', { name: text('components_structure.wizard.submit') }),
    );
  },
};

const ALL = [...DIRECT, DECLARATION_FORM];

/** Lo escrito se conserva: en el campo, o en el resumen del asistente de la declaración. */
function expectKept(form: Form, written: string) {
  if (form === DECLARATION_FORM) {
    const wizard = screen.getByRole('region', { name: text('scr09.unclosable.title') });
    // El resumen pinta el texto con los espacios de los extremos plegados.
    expect(within(wizard).getAllByText(written.trim()).length).toBeGreaterThan(0);
    return;
  }
  expect(form.textbox().value).toBe(written);
}

describe.each(ALL)('SCR-09 $name: el motivo real del rechazo', { timeout: 60_000 }, (form) => {
  it('una atribución a persona: la sugerencia es el motivo principal, con la pista de minúsculas', async () => {
    const state = install(form);
    const user = await form.open();
    const suggestion = 'Sugerencia sintética del patrón 580';
    state.scripted = () =>
      apiError('invalid_request', 400, {
        detailCode: 'loop_person_attribution_detected',
        rejection: { code: 'person_attribution_detected', suggestion },
      });
    await send(form, user, `${WITH_NAMES}\n`);
    const alert = await rejection(form, () => (form === DECLARATION_FORM ? null : form.textbox()));
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(suggestion);
    expect(alert.querySelector('[data-part="rejection-code"]')?.textContent).toBe(
      text('components_assistant.rejection.person_attribution_detected'),
    );
    expect(alert.querySelector('[data-part="rejection-suggestion"]')?.textContent).toBe(suggestion);
    expect(alert.querySelector('[data-part="lowercase_hint"]')?.getAttribute('data-text-key')).toBe(
      'components_forms.reason.lowercase_hint',
    );
    expectKept(form, `${WITH_NAMES}\n`);
    expect(state.bodies.map((body) => body[form.field])).toEqual([WITH_NAMES]);
  });

  it('un término bloqueado: el término y la sugerencia de la respuesta', async () => {
    const state = install(form);
    const user = await form.open();
    const term = 'término-sintético-580';
    const suggestion = 'alternativa sintética 580';
    state.scripted = () =>
      apiError('invalid_request', 400, {
        detailCode: 'loop_blocked_vocabulary_term',
        rejection: { code: 'blocked_vocabulary_term', term, suggestion },
      });
    await send(form, user, TEXT);
    const alert = await rejection(form, () => (form === DECLARATION_FORM ? null : form.textbox()));
    expect(alert.querySelector('[data-part="message"]')?.textContent).toBe(
      label('loop_detail_code', 'loop_blocked_vocabulary_term'),
    );
    expect(alert.querySelector('[data-part="rejection-term"]')?.textContent).toBe(term);
    expect(alert.querySelector('[data-part="rejection-suggestion"]')?.textContent).toBe(suggestion);
    expectKept(form, TEXT);
  });

  it('un 400 sin detail_code: el texto de revisión con el correlation_id, nunca el genérico', async () => {
    const state = install(form);
    const user = await form.open();
    const response = apiError('invalid_request', 400);
    const body = (await response.clone().json()) as { correlation_id: string };
    state.scripted = () => response.clone();
    await send(form, user, TEXT);
    let alert: HTMLElement | null = null;
    await waitFor(() => {
      alert = serverAlert();
      expect(alert).not.toBeNull();
    }, WAIT);
    const found = alert as unknown as HTMLElement;
    expect(found.getAttribute('data-text-key')).toBe('components_forms.reason.review');
    expect(found.querySelector('[data-part="message"]')?.textContent).toBe(
      text('components_forms.reason.review'),
    );
    expect(found.querySelector('[data-part="correlation_id"]')?.textContent).toBe(
      body.correlation_id,
    );
    expectKept(form, TEXT);
  });
});

describe.each(DIRECT)('SCR-09 $name: validar mientras se escribe', { timeout: 60_000 }, (form) => {
  it('un Enter final se envía recortado', async () => {
    const state = install(form);
    const user = await form.open();
    await send(form, user, `${TEXT}\n`);
    await waitFor(() => {
      expect(state.bodies.map((body) => body[form.field])).toEqual([TEXT]);
    }, WAIT);
    expect(serverAlert()).toBeNull();
  });

  it('un salto intermedio avisa en vivo, no envía, y «Unir en una línea» deja un espacio', async () => {
    const state = install(form);
    const user = await form.open();
    const typed = 'Texto sintético del lazo\ncon un salto en medio';
    fireEvent.change(form.textbox(), { target: { value: typed } });
    const warningId = form
      .textbox()
      .getAttribute('aria-describedby')
      ?.split(' ')
      .find((id) => document.getElementById(id)?.getAttribute('data-part') === 'line_break');
    const warning = document.getElementById(warningId ?? '');
    expect(warning?.querySelector('[data-text-key]')?.getAttribute('data-text-key')).toBe(
      'validation.free_text.line_break',
    );
    await form.submit(user);
    await act(async () => {
      await new Promise((done) => {
        setTimeout(done, 50);
      });
    });
    expect(state.bodies).toEqual([]);
    expect(form.textbox().value).toBe(typed);
    await user.click(
      within(warning ?? document.body).getByRole('button', {
        name: text('components_forms.reason.join_lines'),
      }),
    );
    expect(form.textbox().value).toBe('Texto sintético del lazo con un salto en medio');
    await form.submit(user);
    await waitFor(() => {
      expect(state.bodies.map((body) => body[form.field])).toEqual([
        'Texto sintético del lazo con un salto en medio',
      ]);
    }, WAIT);
  });

  it('un texto más corto que el mínimo no envía y lo dice en el campo', async () => {
    const state = install(form);
    const user = await form.open();
    await send(form, user, 'Catorce letras');
    await waitFor(() => {
      const describedBy = form.textbox().getAttribute('aria-describedby')?.split(' ') ?? [];
      const keys = describedBy.map((id) =>
        document.getElementById(id)?.getAttribute('data-text-key'),
      );
      expect(keys).toContain('validation.free_text.too_short');
    }, WAIT);
    await act(async () => {
      await new Promise((done) => {
        setTimeout(done, 50);
      });
    });
    expect(state.bodies).toEqual([]);
    expect(form.textbox().value).toBe('Catorce letras');
  });
});

describe('SCR-09 textos secundarios', { timeout: 60_000 }, () => {
  it('declaración: el motivo del rechazo y la medida compensatoria se envían recortados', async () => {
    const state = install(DECLARATION_FORM);
    const user = await DECLARATION_FORM.open();
    await send(DECLARATION_FORM, user, TEXT);
    await waitFor(() => {
      expect(state.bodies).toHaveLength(1);
    }, WAIT);
    expect({
      rejection_reason: state.bodies[0]?.rejection_reason,
      compensatory_measure: state.bodies[0]?.compensatory_measure,
    }).toEqual({ rejection_reason: DESCRIPTION, compensatory_measure: DESCRIPTION });
  });

  it('evidencia: la nota de verificación en blanco es null y escrita va recortada', async () => {
    const state = install(EVIDENCE);
    const user = await EVIDENCE.open();
    const note = () =>
      screen.getAllByRole('textbox', {
        name: `${text('scr09.closure.verification')} (${text('components_forms.reason.optional')})`,
      })[0] as HTMLTextAreaElement;
    fireEvent.change(note(), { target: { value: ' \n ' } });
    await send(EVIDENCE, user, TEXT);
    await waitFor(() => {
      expect(state.bodies).toHaveLength(1);
    }, WAIT);
    expect(state.bodies[0]?.verification_note).toBeNull();
    fireEvent.change(note(), { target: { value: 'Nota sintética de verificación\n' } });
    await EVIDENCE.submit(user);
    await waitFor(() => {
      expect(state.bodies).toHaveLength(2);
    }, WAIT);
    expect(state.bodies[1]?.verification_note).toBe('Nota sintética de verificación');
  });

  it('acción: el resultado esperado con un error local no envía', async () => {
    const state = install(ACTION_FORM);
    const user = await ACTION_FORM.open();
    const expected = screen.getByRole('textbox', {
      name: `${text('scr09.loop.expected_outcome')} (${text('components_forms.reason.optional')})`,
    });
    fireEvent.change(expected, { target: { value: 'Resultado <b>sintético</b>' } });
    await send(ACTION_FORM, user, TEXT);
    await waitFor(() => {
      const keys = (expected.getAttribute('aria-describedby')?.split(' ') ?? []).map((id) =>
        document.getElementById(id)?.getAttribute('data-text-key'),
      );
      expect(keys).toContain('validation.free_text.markup');
    }, WAIT);
    await act(async () => {
      await new Promise((done) => {
        setTimeout(done, 50);
      });
    });
    expect(state.bodies).toEqual([]);
  });

  it('revisión del incerrable: la acción nueva se envía recortada y se firma como se envía', async () => {
    const state = install(REVIEW);
    const user = await REVIEW.open();
    const region = reviewRegion();
    await user.selectOptions(
      within(region).getByLabelText(text('scr09.unclosable.outcome')),
      'converted_to_action',
    );
    await user.selectOptions(
      within(region).getByLabelText(text('scr09.reopening.action_type')),
      'engineering',
    );
    fireEvent.change(
      within(region).getByRole('textbox', {
        name: reasonName('scr09.reopening.action_description'),
      }),
      { target: { value: `${DESCRIPTION}\n` } },
    );
    fireEvent.change(REVIEW.textbox(), { target: { value: TEXT } });
    const signed = within(region).getByText(
      (_content, element) => element?.getAttribute('data-part') === 'text_to_sign',
    );
    expect(signed.textContent).toBe(
      [label('unclosable_review_outcome', 'converted_to_action'), TEXT, DESCRIPTION].join('\n'),
    );
    await REVIEW.submit(user);
    await waitFor(() => {
      expect(state.bodies).toHaveLength(1);
    }, WAIT);
    expect(state.bodies[0]?.action).toEqual({
      action_type: 'engineering',
      action_description: DESCRIPTION,
      expected_outcome: null,
      assignee_user_id: USER,
      deadline_days: null,
    });
  });
});

describe('SCR-09 reapertura: la acción nueva', { timeout: 60_000 }, () => {
  it('se envía con sus textos recortados', async () => {
    const state = install(REOPENING);
    const user = await REOPENING.open();
    await send(REOPENING, user, TEXT);
    await waitFor(() => {
      expect(state.bodies).toHaveLength(1);
    }, WAIT);
    expect(state.bodies[0]).toEqual({
      reason_text: TEXT,
      action_type: 'engineering',
      action_description: DESCRIPTION,
      expected_outcome: null,
      assignee_user_id: USER,
      deadline_days: null,
    });
  });
});

describe('SCR-09 límites de los textos que el esquema generado no publica', () => {
  const fixtures = JSON.parse(
    readFileSync(
      resolve(
        dirname(fileURLToPath(import.meta.url)),
        '../../../../backend/openapi/u05-fixtures.json',
      ),
      'utf8',
    ),
  ) as { free_text: { routes: { route: string; min_length: number; max_length: number }[] } };
  const limits = (route: string) => {
    const found = fixtures.free_text.routes.find((candidate) => candidate.route === route);
    return found === undefined
      ? undefined
      : { minLength: found.min_length, maxLength: found.max_length };
  };

  it('el motivo de la clasificación es el de U-04 (igualdad con el corpus)', () => {
    expect(CLASSIFICATION_REASON_LIMITS).toEqual(limits('classification/reason_text'));
  });

  it.each([
    'reopening/reason_text',
    'unclosable_state_declared/evaluated_alternative',
    'unclosable_state_declared/rejection_reason',
    'unclosable_state_declared/compensatory_measure',
    'unclosable_review/review_note',
  ])('LONG_TEXT es el de %s (igualdad con el corpus)', (route) => {
    expect(LONG_TEXT).toEqual(limits(route));
  });
});

describe('SCR-09 axe con el aviso de salto de línea y un rechazo anclado', () => {
  it('sin violaciones', { timeout: 60_000 }, async () => {
    const state = install(ACTION_FORM);
    const user = await ACTION_FORM.open();
    state.scripted = () =>
      apiError('invalid_request', 400, {
        detailCode: 'loop_blocked_vocabulary_term',
        rejection: { code: 'blocked_vocabulary_term', term: 'término', suggestion: 'alternativa' },
      });
    await send(ACTION_FORM, user, TEXT);
    await rejection(ACTION_FORM);
    fireEvent.change(
      screen.getByRole('textbox', {
        name: `${text('scr09.loop.expected_outcome')} (${text('components_forms.reason.optional')})`,
      }),
      { target: { value: 'Resultado sintético\ncon un salto' } },
    );
    expect(await axe(document.body)).toHaveNoViolations();
  });
});

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import {
  CLASSIFICATION_REASON_LIMITS,
  type ClassificationBody,
  type ClassificationItem,
} from '@/ports/scr09Classify';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
import {
  classification,
  detail,
  FINDING,
  history,
  INSTANT,
  renderScreen,
  WAIT,
} from './classify_support';

const REASON = 'Reflejo del sol sobre la guarda en la tarde, revisado en el clip';
const REASON_NAME = `${text('scr09.classify.reason_text')} (${text('components_forms.reason.required')})`;
const validators = openApiValidators(
  (readAppYaml().components as { schemas: Record<string, unknown> }).schemas,
);

/** Servidor con historial propio: cada POST aceptado añade un registro nuevo al final. */
function loopServer(initial: ClassificationItem[], state = 'open') {
  const items = [...initial];
  const bodies: unknown[] = [];
  const reads = { detail: 0, history: 0 };
  server.use(
    http.get(`http://localhost/loop/findings/${FINDING}`, () => {
      reads.detail++;
      return HttpResponse.json(
        detail({
          lifecycle_state: state,
          classifications: items,
          classification_count: items.length,
        }),
      );
    }),
    http.get(`http://localhost/loop/findings/${FINDING}/classifications`, () => {
      reads.history++;
      return HttpResponse.json(history(items));
    }),
    http.post(`http://localhost/loop/findings/${FINDING}/classifications`, async ({ request }) => {
      const body = (await request.json()) as ClassificationBody;
      bodies.push(body);
      const validate = validators.component('ClassificationBody');
      if (!validate(body)) return apiError('invalid_request', 400);
      const next = classification(items.length + 1, body.outcome ?? '');
      items.push({
        ...next,
        reason_category: body.reason_category ?? '',
        reason_text: body.reason_text ?? '',
        reinforced_reason: body.reinforced_reason,
      });
      return HttpResponse.json(
        {
          finding_state_id: FINDING,
          classification_id: next.classification_id,
          record_id: next.record_id,
          sequence_in_subject: next.sequence_in_subject,
          outcome: next.outcome,
          reason_category: next.reason_category,
          is_reclassification: next.is_reclassification,
          reinforced_reason: body.reinforced_reason,
          effect_on_loop: 'loop_suspended',
          lifecycle_state: 'suspended_by_reclassification',
          classification_count: items.length,
          closed_with_later_different_classification: false,
          classified_at: INSTANT,
          time_aggregated: false,
        },
        { status: 201 },
      );
    }),
  );
  return { items, bodies, reads };
}

async function load() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: text('scr09.classify.load') }, WAIT));
  await screen.findByRole('form', { name: text('scr09.classify.form') }, WAIT);
  return user;
}

describe('SCR-09 clasificación', { timeout: 40_000 }, () => {
  it('los límites del motivo son los de classification/reason_text del corpus exportado', () => {
    const path = resolve(
      dirname(fileURLToPath(import.meta.url)),
      '../../../../backend/openapi/u05-fixtures.json',
    );
    const fixtures = JSON.parse(readFileSync(path, 'utf8')) as {
      free_text: { routes: { route: string; min_length: number; max_length: number }[] };
    };
    const route = fixtures.free_text.routes.find(
      (item) => item.route === 'classification/reason_text',
    );
    expect(CLASSIFICATION_REASON_LIMITS).toEqual({
      minLength: route?.min_length,
      maxLength: route?.max_length,
    });
  });

  it('no pide lo auditado al montar; solo al pulsar consultar', async () => {
    const { reads } = loopServer([classification(1)]);
    const { cache } = await renderScreen('classify');
    await screen.findByRole('button', { name: text('scr09.classify.load') }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(reads).toEqual({ detail: 0, history: 0 });
    await load();
    expect(reads).toEqual({ detail: 1, history: 1 });
  });

  it('reclasificar con el lazo abierto avisa la suspensión, pide el motivo reforzado y registra uno nuevo sin borrar el anterior', async () => {
    const { bodies } = loopServer([classification(1)], 'action_assigned');
    const { container, cache } = await renderScreen('classify');
    const user = await load();
    const list = screen.getByRole('list', { name: text('scr09.classify.history') });
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
    expect(container.querySelector('[data-part="suspends"]')?.textContent).toBe(
      text('scr09.classify.suspends'),
    );
    await user.click(
      screen.getByRole('radio', { name: label('classification_outcome', 'false_positive') }),
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr09.classify.reason_category') }),
      'reflection_or_glare',
    );
    await user.type(screen.getByRole('textbox', { name: REASON_NAME }), REASON);
    await user.click(screen.getByRole('checkbox', { name: text('scr09.classify.reinforced') }));
    await user.click(screen.getByRole('button', { name: text('scr09.classify.submit') }));
    await waitFor(() => {
      expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    }, WAIT);
    expect(bodies).toEqual([
      {
        outcome: 'false_positive',
        reason_category: 'reflection_or_glare',
        reason_text: REASON,
        reinforced_reason: true,
      },
    ]);
    const items = within(list).getAllByRole('listitem');
    expect(
      items.map((item) => item.querySelector('[data-part="reason_text"]')?.textContent),
    ).toEqual([classification(1).reason_text, REASON]);
    const receipt = await screen.findByText(
      `${text('scr09.classify.receipt')} 2: ${label('classification_outcome', 'false_positive')}`,
      { selector: '[data-part="record"]' },
      WAIT,
    );
    expect(receipt.closest('[role="status"]')?.getAttribute('data-role-in-use')).toBe(
      'coordinator_sst',
    );
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    // BR-APP-24: los únicos controles son consultar, actualizar y registrar; ningún editar o borrar.
    const allowed = new Set([
      text('scr09.classify.load'),
      text('scr09.classify.submit'),
      text('components_status.refresh.button'),
    ]);
    for (const button of screen.getAllByRole('button'))
      expect(allowed.has(button.textContent)).toBe(true);
    for (const link of screen.getAllByRole('link'))
      expect([text('scr09.classify.title'), text('scr09.loop.title')]).toContain(link.textContent);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('un motivo rechazado por el servidor se ancla al campo y conserva lo escrito', async () => {
    loopServer([]);
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/classifications`, () =>
        apiError('invalid_request', 400, { detailCode: 'loop_person_attribution_detected' }),
      ),
    );
    await renderScreen('classify');
    const user = await load();
    expect(screen.queryByRole('checkbox')).toBeNull();
    await user.click(
      screen.getByRole('radio', { name: label('classification_outcome', 'confirmed') }),
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr09.classify.reason_category') }),
      'procedure_gap',
    );
    const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: REASON_NAME });
    await user.type(field, REASON);
    await user.click(screen.getByRole('button', { name: text('scr09.classify.submit') }));
    await waitFor(() => {
      expect(field.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    const notice = document.getElementById(
      field.getAttribute('aria-describedby')?.split(' ')[1] ?? '',
    );
    expect(notice?.getAttribute('data-code')).toBe('invalid_request');
    expect(notice?.querySelector('[data-part="message"]')?.textContent).toBe(
      label('loop_detail_code', 'loop_person_attribution_detected'),
    );
    expect(field.value).toBe(REASON);
    const category = screen.getByRole<HTMLSelectElement>('combobox', {
      name: text('scr09.classify.reason_category'),
    });
    expect(category.value).toBe('procedure_gap');
  });
});

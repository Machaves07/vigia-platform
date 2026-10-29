/**
 * SCR-05 · políticas de la planta (IF-11 pasos 2 y 4):
 *
 * - `/plants/{plant_id}/policy`: la política de incerrables vigente y la carga de la versión
 *   siguiente con documento firmado (`plant_policy`), fecha en la zona horaria de la planta,
 *   firmante y referencia al concepto del abogado; sin documento no se envía nada.
 * - Política de firmantes: `PUT` con roles y mínimo; `catalog_fewer_than_three` y
 *   `catalog_workers_representation_missing` llegan del servidor y se anclan (la pantalla no los
 *   impone).
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Gates from '@/pages/scr05/gates';
import Policy from '@/pages/scr05/policy';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { apiError } from '../../msw/handlers';
import { changeDate } from '../../support/dateFields';
import { server } from '../../msw/server';
import {
  gatesFixture,
  ORIGIN,
  parentOf,
  part,
  PLANT,
  plantPolicy,
  serveDocuments,
  serveReads,
  signatoryPolicy,
  syntheticFile,
  WAIT,
} from './support';

async function renderPolicy(loaded: boolean) {
  serveReads({ policy: plantPolicy(loaded) });
  const fixture = await gatesFixture(`/plants/${PLANT}/policy`);
  const view = render(<Policy />, { wrapper: fixture.Wrapper });
  const form = await waitFor(() => {
    const node = view.container.querySelector('[data-part="policy_form"]');
    expect(node).not.toBeNull();
    return node as HTMLElement;
  }, WAIT);
  return { ...view, form };
}

describe('SCR-05 política de incerrables', { timeout: 60_000 }, () => {
  it('sin política: la versión siguiente es 1 y sin documento no se envía nada', async () => {
    const posts: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/plants/:plant_id/policy`, async ({ request }) => {
        posts.push(await request.json());
        return HttpResponse.json(plantPolicy(true), { status: 201 });
      }),
    );
    const { container, form } = await renderPolicy(false);
    expect(screen.getByText(text('scr05.policy.not_loaded'))).toBeDefined();
    expect(form.querySelector('[data-part="next_version"]')?.textContent).toBe('1');
    fireEvent.click(within(form).getByRole('button', { name: text('scr05.policy.submit') }));
    await waitFor(() => {
      expect(form.querySelectorAll('[data-part="local_error"]')).toHaveLength(5);
    }, WAIT);
    expect(posts).toEqual([]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('carga la versión siguiente con documento firmado, fecha de la planta y concepto legal', async () => {
    const grants = serveDocuments();
    const posts: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/plants/:plant_id/policy`, async ({ request }) => {
        posts.push(await request.json());
        return HttpResponse.json({ ...plantPolicy(true), version: 3 }, { status: 201 });
      }),
    );
    const user = userEvent.setup();
    const { form } = await renderPolicy(true);
    // La vigente (versión 2) se muestra con su firmante y su concepto.
    const current = part(document, 'plant_policy');
    expect(within(current).getByText('CONCEPTO-SINT-002')).toBeDefined();
    expect(within(current).getByText('Gerencia sintética')).toBeDefined();
    expect(form.querySelector('[data-part="next_version"]')?.textContent).toBe('3');
    changeDate(within(form).getByLabelText(text('scr05.policy.signed_at')), '2026-01-15');
    fireEvent.change(within(form).getByLabelText(text('scr05.policy.signed_by')), {
      target: { value: 'Gerencia sintética' },
    });
    fireEvent.change(within(form).getByLabelText(text('scr05.policy.legal_opinion')), {
      target: { value: 'CONCEPTO-SINT-003' },
    });
    fireEvent.change(within(form).getByLabelText(text('scr05.policy.criteria')), {
      target: { value: 'Criterios sintéticos de la versión 3' },
    });
    await user.upload(
      within(form).getByLabelText(text('scr05.policy.document'), { selector: 'input' }),
      syntheticFile('politica.pdf', 'application/pdf'),
    );
    await user.click(within(form).getByRole('button', { name: text('media.upload.submit') }));
    await within(form).findByText(`${text('media.upload.done')}: politica.pdf`, {}, WAIT);
    fireEvent.click(within(form).getByRole('button', { name: text('scr05.policy.submit') }));
    await screen.findByText(text('scr05.policy.recorded'), {}, WAIT);
    expect(grants).toHaveLength(1);
    expect(grants[0]).toMatchObject({ kind: 'plant_policy', plant_id: PLANT });
    expect(posts).toHaveLength(1);
    const body = posts[0] as Record<string, unknown>;
    expect(body).toMatchObject({
      version: 3,
      // Medianoche del 15 de enero en America/Bogota (UTC−5).
      signed_at: '2026-01-15T05:00:00.000Z',
      signed_by_display_name: 'Gerencia sintética',
      legal_opinion_reference: 'CONCEPTO-SINT-003',
      criteria_summary_es: 'Criterios sintéticos de la versión 3',
    });
    expect((body.document_ref as { content_type: string }).content_type).toBe('application/pdf');
  });

  it('una versión que no es la siguiente responde conflict y queda anclada', async () => {
    serveDocuments();
    server.use(http.post(`${ORIGIN}/plants/:plant_id/policy`, () => apiError('conflict', 409)));
    const user = userEvent.setup();
    const { form } = await renderPolicy(true);
    changeDate(within(form).getByLabelText(text('scr05.policy.signed_at')), '2026-01-15');
    for (const [key, value] of [
      ['scr05.policy.signed_by', 'Gerencia sintética'],
      ['scr05.policy.legal_opinion', 'CONCEPTO-SINT-003'],
      ['scr05.policy.criteria', 'Criterios sintéticos'],
    ] as const) {
      fireEvent.change(within(form).getByLabelText(text(key)), { target: { value } });
    }
    await user.upload(
      within(form).getByLabelText(text('scr05.policy.document'), { selector: 'input' }),
      syntheticFile('politica.pdf', 'application/pdf'),
    );
    await user.click(within(form).getByRole('button', { name: text('media.upload.submit') }));
    await within(form).findByText(`${text('media.upload.done')}: politica.pdf`, {}, WAIT);
    fireEvent.click(within(form).getByRole('button', { name: text('scr05.policy.submit') }));
    const alert = await waitFor(() => {
      const node = form.parentElement?.querySelector('[role="alert"][data-code="conflict"]');
      expect(node).not.toBeUndefined();
      expect(node).not.toBeNull();
      return node as HTMLElement;
    }, WAIT);
    expect(alert.textContent).not.toBe('');
    expect(screen.queryByText(text('scr05.policy.recorded'))).toBeNull();
  });
});

describe('SCR-05 política de firmantes', { timeout: 60_000 }, () => {
  async function renderPlant() {
    serveReads({ signatories: signatoryPolicy() });
    const fixture = await gatesFixture(`/plants/${PLANT}/gates`);
    const view = render(<Gates />, { wrapper: fixture.Wrapper });
    const form = await waitFor(() => {
      const node = view.container.querySelector('[data-part="signatory_form"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    }, WAIT);
    return { ...view, form };
  }

  it('muestra la vigente y envía roles y mínimo', async () => {
    const puts: unknown[] = [];
    server.use(
      http.put(`${ORIGIN}/plants/:plant_id/signatory-policy`, async ({ request }) => {
        puts.push(await request.json());
        return HttpResponse.json(signatoryPolicy());
      }),
    );
    const { container, form } = await renderPlant();
    const current = part(container, 'signatory_policy');
    expect(within(current).getByText(label('role', 'copasst'))).toBeDefined();
    fireEvent.click(within(form).getByRole('checkbox', { name: label('role', 'administrator') }));
    fireEvent.change(within(form).getByLabelText(text('scr05.signatories.minimum')), {
      target: { value: '4' },
    });
    fireEvent.click(within(form).getByRole('button', { name: text('scr05.signatories.save') }));
    await within(parentOf(form)).findByText(text('scr05.signatories.saved'), {}, WAIT);
    expect(puts).toEqual([
      {
        required_roles: ['coordinator_sst', 'plant_manager', 'administrator', 'copasst'],
        minimum: 4,
      },
    ]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it.each([
    ['catalog_fewer_than_three', '2'],
    ['catalog_workers_representation_missing', '3'],
  ])('%s llega del servidor y se ancla (la pantalla no lo impone)', async (detailCode, minimum) => {
    const puts: unknown[] = [];
    server.use(
      http.put(`${ORIGIN}/plants/:plant_id/signatory-policy`, async ({ request }) => {
        puts.push(await request.json());
        return apiError('invalid_request', 422, { detailCode });
      }),
    );
    const { form } = await renderPlant();
    if (detailCode === 'catalog_workers_representation_missing')
      fireEvent.click(within(form).getByRole('checkbox', { name: label('role', 'copasst') }));
    fireEvent.change(within(form).getByLabelText(text('scr05.signatories.minimum')), {
      target: { value: minimum },
    });
    fireEvent.click(within(form).getByRole('button', { name: text('scr05.signatories.save') }));
    const alert = await within(parentOf(form)).findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('invalid_request');
    expect(puts).toHaveLength(1);
    expect((puts[0] as { minimum: number }).minimum).toBe(Number(minimum));
  });
});

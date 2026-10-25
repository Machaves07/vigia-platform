import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { validateDeclaredStandard } from '@/contracts';
import { StandardForm } from '@/pages/scr04/catalog/StandardForm';
import { parsePredicate, predicateTemplate } from '@/ports/scr04Catalog';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { server } from '../../msw/server';
import { fixture, fill, next, payload, PLANT, PREDICATE, version, ZONE } from './support';

async function declaration() {
  const { Wrapper } = await fixture();
  render(<StandardForm zoneId={ZONE} plantId={PLANT} />, { wrapper: Wrapper });
  await screen.findByRole('option', { name: label('predicate_family', 'dwell') });
  fill('scr04.catalog.family', 'dwell');
  fill('scr04.catalog.title_es', 'Estándar de prueba');
  fill('scr04.catalog.declared_text', 'Presencia con energía afirmada.');
  await next();
}
async function send() {
  fill('scr04.catalog.predicate', JSON.stringify(PREDICATE));
  await next();
  fill('scr04.catalog.reason', 'Motivo sintético suficiente');
  await next();
  fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.submit') }));
}
describe('SCR-04 declaración', { timeout: 30_000 }, () => {
  it.each(['coexistence', 'guard_bypass', 'dwell', 'startup_transition'] as const)(
    'plantilla válida de %s',
    (family) => {
      expect(parsePredicate(family, predicateTemplate(family))).not.toBeNull();
    },
  );
  it.each([
    {},
    { ...PREDICATE, person_id: 'identificador-sintetico' },
    { all_of: [{ presence: false }], min_duration_ms: 0 },
    { ...PREDICATE, min_duration_ms: 3600001 },
    { ...PREDICATE, min_duration_ms: '1000' },
  ])('ancla predicado inválido y no lo envía: %j', async (predicate) => {
    let calls = 0;
    await declaration();
    server.use(
      http.post(`http://localhost/zones/${ZONE}/standards`, () => {
        calls++;
        return HttpResponse.json(version(), { status: 201 });
      }),
    );
    fill('scr04.catalog.predicate', JSON.stringify(predicate));
    expect(
      screen.getByLabelText(text('scr04.catalog.predicate')).getAttribute('aria-invalid'),
    ).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe(text('scr04.catalog.predicate_invalid'));
    expect(
      screen
        .getByRole('button', { name: text('components_structure.wizard.next') })
        .hasAttribute('disabled'),
    ).toBe(true);
    expect(calls).toBe(0);
  });
  it('envía el predicado intacto; MSW valida el estándar con Ajv del contrato', async () => {
    const sent: unknown[] = [];
    await declaration();
    server.use(
      http.post(`http://localhost/zones/${ZONE}/standards`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        sent.push(body);
        expect(body.predicate).toEqual(PREDICATE);
        expect(
          validateDeclaredStandard({
            ...payload().standards[0],
            family: body.family,
            title_es: body.title_es,
            declared_text: body.declared_text,
            predicate: body.predicate,
          }),
        ).toBe(true);
        return HttpResponse.json(version(2), { status: 201 });
      }),
    );
    fill('scr04.catalog.predicate', JSON.stringify(PREDICATE));
    await next();
    fill('scr04.catalog.reason', 'Motivo sintético suficiente');
    await next();
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.wizard.submit') }),
    );
    await waitFor(() => {
      expect(sent).toHaveLength(1);
    });
    expect(await screen.findByText(text('scr04.catalog.saved'))).toBeDefined();
  });
  it.each([1000, 3600000])(
    'acepta el borde temporal %i; rechaza familia cruzada y JSON roto',
    (duration) => {
      const predicate = { ...PREDICATE, min_duration_ms: duration };
      expect(parsePredicate('dwell', JSON.stringify(predicate))).toEqual(predicate);
      expect(parsePredicate('startup_transition', JSON.stringify(predicate))).toBeNull();
      expect(parsePredicate('coexistence', '{')).toBeNull();
    },
  );
  it.each(['<b>zona</b>', 'zona\u200b', 'a'.repeat(4001)])(
    'bloquea texto declarado estructuralmente inválido',
    async (value) => {
      const { Wrapper } = await fixture();
      render(<StandardForm zoneId={ZONE} plantId={PLANT} />, { wrapper: Wrapper });
      await screen.findByRole('option', { name: label('predicate_family', 'dwell') });
      fill('scr04.catalog.family', 'dwell');
      fill('scr04.catalog.title_es', 'Estándar');
      fill('scr04.catalog.declared_text', value);
      expect(
        screen
          .getByRole('button', { name: text('components_structure.wizard.next') })
          .hasAttribute('disabled'),
      ).toBe(true);
    },
  );
  it('acepta exactamente 4 000 caracteres y rechaza 121 en el título', async () => {
    const { Wrapper } = await fixture();
    render(<StandardForm zoneId={ZONE} plantId={PLANT} />, { wrapper: Wrapper });
    await screen.findByRole('option', { name: label('predicate_family', 'dwell') });
    fill('scr04.catalog.family', 'dwell');
    fill('scr04.catalog.title_es', 't'.repeat(120));
    fill('scr04.catalog.declared_text', 'd'.repeat(4000));
    await waitFor(() => {
      expect(
        screen
          .getByRole('button', { name: text('components_structure.wizard.next') })
          .hasAttribute('disabled'),
      ).toBe(false);
    });
    fill('scr04.catalog.title_es', 't'.repeat(121));
    expect(
      screen
        .getByRole('button', { name: text('components_structure.wizard.next') })
        .hasAttribute('disabled'),
    ).toBe(true);
  });
  it.each([
    ['catalog_family_not_admitted', 'scr04.catalog.family', 2],
    ['catalog_predicate_invalid', 'scr04.catalog.predicate', 1],
    ['catalog_free_text_rejected', 'scr04.catalog.declared_text', 2],
  ] as const)(
    'conserva el borrador y ancla %s al volver al campo',
    async (code, field, stepsBack) => {
      await declaration();
      server.use(
        http.post(`http://localhost/zones/${ZONE}/standards`, () =>
          HttpResponse.json(
            { code: 'invalid_request', detail_code: code, correlation_id: PLANT },
            { status: 400 },
          ),
        ),
      );
      await send();
      await screen.findByText(label('catalog_detail_code', code));
      for (let index = 0; index <= stepsBack; index++) {
        fireEvent.click(
          screen.getByRole('button', { name: text('components_structure.wizard.back') }),
        );
      }
      expect(screen.getByLabelText(text(field)).getAttribute('aria-invalid')).toBe('true');
      expect(screen.getByLabelText<HTMLInputElement>(text(field)).value).not.toBe('');
    },
  );
  it('zona sin cámaras muestra rechazo anclado al formulario, sin ocultar la acción', async () => {
    await declaration();
    server.use(
      http.post(`http://localhost/zones/${ZONE}/standards`, () =>
        HttpResponse.json(
          {
            code: 'invalid_request',
            detail_code: 'catalog_zone_without_cameras',
            correlation_id: PLANT,
          },
          { status: 400 },
        ),
      ),
    );
    await send();
    expect(
      await screen.findByText(label('catalog_detail_code', 'catalog_zone_without_cameras')),
    ).toBeDefined();
    await waitFor(() => {
      expect(
        screen
          .getByRole('button', { name: text('components_structure.wizard.submit') })
          .hasAttribute('disabled'),
      ).toBe(false);
    });
  });
});

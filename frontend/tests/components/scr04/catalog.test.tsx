import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr04/catalog';
import { Changes } from '@/pages/scr04/catalog/Changes';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { CatalogView } from '@/pages/scr04/catalog/CatalogView';
import { admittedFamilies, catalogPayload, catalogPort } from '@/ports/scr04Catalog';
import registration from '../../views/scr04';
import { assertNoPersonData, assertOrganizationIsolation } from '../../support/viewProperties';
import { settle } from '../../support/queryCache';
import { server } from '../../msw/server';
import {
  CLIENT,
  detail,
  fixture,
  fill,
  next,
  ORG,
  payload,
  PLANT,
  STANDARD,
  version,
  ZONE,
} from './catalogSupport';

describe('SCR-04 catálogo', { timeout: 30_000 }, () => {
  it('RV-07: propiedades de privacidad y organización sobre la vista real', () => {
    assertNoPersonData(registration.catalog);
    expect(assertOrganizationIsolation(registration.catalog)).toBeGreaterThan(0);
  }, 180_000);
  it('rechaza catálogos de otra zona u organización antes de pintar acciones', () => {
    expect(() => catalogPayload(version(), PLANT, ORG)).toThrow('Catálogo de otra zona.');
    expect(() => catalogPayload(version(), ZONE, PLANT)).toThrow('Catálogo de otra organización.');
    expect(catalogPayload(version(), ZONE, PLANT, ORG)).toEqual(payload());
  });
  it('familias admitidas: recorre páginas sin ofrecer rechazos ni repetir cursores', async () => {
    await fixture();
    const cursors: (string | null)[] = [];
    server.use(
      http.get(`http://localhost/plants/${PLANT}/admissions`, ({ request }) => {
        const after = new URL(request.url).searchParams.get('after');
        cursors.push(after);
        return HttpResponse.json({
          admissions:
            after === null
              ? [{ family: 'coexistence', result: 'rejected' }]
              : [{ family: 'dwell', result: 'admitted' }],
          next_after: after === null ? 'cursor-sintetico' : null,
        });
      }),
    );
    expect(await admittedFamilies(PLANT, new AbortController().signal)).toEqual(['dwell']);
    expect(cursors).toEqual([null, 'cursor-sintetico']);
    server.use(
      http.get(`http://localhost/plants/${PLANT}/admissions`, () =>
        HttpResponse.json({ admissions: [], next_after: 'cursor-repetido' }),
      ),
    );
    await expect(admittedFamilies(PLANT, new AbortController().signal)).rejects.toThrow(
      'Cursor de admisiones repetido.',
    );
  });
  it('declara todas las operaciones como fixed, con permiso y sin editar/borrar', () => {
    expect(catalogPort.operations).toHaveLength(10);
    for (const operation of catalogPort.operations) {
      expect(operation.status).toBe('fixed');
      expect(operation.api_operation_id).not.toBeNull();
      expect(['session', 'catalog.read', 'catalog.manage']).toContain(operation.permission);
    }
  });
  it('muestra vigente sin editar/borrar versiones y sin infracciones axe serias', async () => {
    const { Wrapper } = await fixture();
    const { container } = render(<Page />, { wrapper: Wrapper });
    expect(await screen.findByRole('heading', { name: 'Estándar sintético 1' })).toBeDefined();
    expect(screen.getByRole('link', { name: 'Planta sintética' }).getAttribute('href')).toBe(
      `/plants/${PLANT}/catalog`,
    );
    await screen.findByRole('table', { name: text('scr04.catalog.history') });
    expect(new Set(screen.getAllByRole('button').map((button) => button.textContent))).toEqual(
      new Set([
        text('components_status.refresh.button'),
        text('scr04.catalog.declare'),
        `${text('scr04.catalog.new_version')} · Estándar sintético 1`,
        text('scr04.catalog.retire'),
        text('scr04.catalog.save_occupancy'),
        text('scr04.catalog.compare'),
      ]),
    );
    const result = await axe(container);
    expect(
      result.violations.filter((item) => item.impact === 'serious' || item.impact === 'critical'),
    ).toEqual([]);
  });
  it.each(['catalog', 'standards'])(
    'catalog.read no ofrece ninguna gestión en la ruta %s',
    async (group) => {
      const { Wrapper } = await fixture(`/zones/${ZONE}/${group}`, {
        permissions: ['catalog.read'],
      });
      render(<Page />, { wrapper: Wrapper });
      await screen.findByRole('heading', { name: 'Estándar sintético 1' });
      await screen.findByRole('table', { name: text('scr04.catalog.history') });
      expect(
        screen
          .getAllByRole('button')
          .map((button) => button.textContent)
          .sort(),
      ).toEqual(
        [
          text('components_status.refresh.button'),
          text('components_status.refresh.button'),
          text('components_status.refresh.button'),
          text('components_status.refresh.button'),
          text('scr04.catalog.compare'),
        ].sort(),
      );
      expect(screen.queryByRole('heading', { name: text('scr04.catalog.declare') })).toBeNull();
      expect(screen.getAllByRole('form').map((form) => form.getAttribute('aria-label'))).toEqual([
        text('scr04.catalog.compare'),
      ]);
    },
  );
  it('crear versión invalida catálogo y regresión y muestra la marca del servidor', async () => {
    const { Wrapper } = await fixture();
    let changed = false;
    let catalogs = 0;
    let regressions = 0;
    server.use(
      http.get(`http://localhost/zones/${ZONE}/catalog`, () => {
        catalogs++;
        return HttpResponse.json(version(changed ? 2 : 1));
      }),
      http.get(`http://localhost/zones/${ZONE}/regression`, () => {
        regressions++;
        return HttpResponse.json({ zone_id: ZONE, state: changed ? 'pending' : 'current' });
      }),
      http.post(
        `http://localhost/zones/${ZONE}/standards/${STANDARD}/versions`,
        async ({ request }) => {
          expect(await request.json()).toMatchObject({
            changes: { title_es: 'Título nuevo' },
            reason_es: 'Motivo sintético suficiente',
          });
          changed = true;
          return HttpResponse.json(version(2), { status: 201 });
        },
      ),
    );
    render(<Page />, { wrapper: Wrapper });
    fireEvent.click(
      await screen.findByRole('button', {
        name: `${text('scr04.catalog.new_version')} · Estándar sintético 1`,
      }),
    );
    fill('scr04.catalog.title_es', 'Título nuevo');
    await next();
    await next();
    const reasons = screen.getAllByLabelText(
      `${text('scr04.catalog.reason')} (${text('components_forms.reason.required')})`,
    );
    fireEvent.change(reasons[1] ?? document.body, {
      target: { value: 'Motivo sintético suficiente' },
    });
    await next();
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.wizard.submit') }),
    );
    expect(
      await screen.findByText(text('components_status.gate.regression_pending')),
    ).toBeDefined();
    await waitFor(() => {
      expect(catalogs).toBe(2);
      expect(regressions).toBe(2);
    });
  });
  it('catálogo de planta muestra la zona y el enlace al catálogo vigente', async () => {
    const { Wrapper } = await fixture(`/plants/${PLANT}/catalog`);
    render(<Page />, { wrapper: Wrapper });
    expect((await screen.findByRole('link', { name: 'Zona sintética' })).getAttribute('href')).toBe(
      `/zones/${ZONE}/catalog`,
    );
    expect(await screen.findByRole('heading', { name: 'Estándar sintético 1' })).toBeDefined();
  });
  it('grupo standards abre el asistente desde la ruta de declaración', async () => {
    const { Wrapper } = await fixture(`/zones/${ZONE}/standards`);
    render(<Page />, { wrapper: Wrapper });
    expect(
      await screen.findByRole('heading', { name: text('scr04.catalog.declare') }),
    ).toBeDefined();
    expect(
      await screen.findByRole('option', { name: label('predicate_family', 'dwell') }),
    ).toBeDefined();
  });
  it.each([15, 480])('publica marca unipersonal con ventana límite %i y motivo', async (window) => {
    const { Wrapper } = await fixture();
    const sent: unknown[] = [];
    server.use(
      http.put(`http://localhost/zones/${ZONE}/catalog/single-occupancy`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(version(2));
      }),
    );
    render(<Changes zoneId={ZONE} version={version()} timeZone="America/Bogota" />, {
      wrapper: Wrapper,
    });
    fill('scr04.catalog.window', String(window));
    fireEvent.click(screen.getByRole('checkbox'));
    fill('scr04.catalog.reason', 'Motivo sintético suficiente');
    fireEvent.click(screen.getByRole('button', { name: text('scr04.catalog.save_occupancy') }));
    await waitFor(() => {
      expect(sent).toEqual([
        {
          single_occupancy: true,
          aggregation_window_minutes: window,
          reason_es: 'Motivo sintético suficiente',
        },
      ]);
    });
  });
  it.each([14, 481, 15.5])('no publica ventana fuera del contrato %s', async (window) => {
    const { Wrapper } = await fixture();
    let calls = 0;
    server.use(
      http.put(`http://localhost/zones/${ZONE}/catalog/single-occupancy`, () => {
        calls++;
        return HttpResponse.json(version(2));
      }),
    );
    render(<Changes zoneId={ZONE} version={version()} timeZone="America/Bogota" />, {
      wrapper: Wrapper,
    });
    const write = vi.spyOn(catalogPort.occupancy, 'write');
    fill('scr04.catalog.window', String(window));
    fill('scr04.catalog.reason', 'Motivo sintético suficiente');
    await act(async () => {
      fireEvent.submit(screen.getByRole('form', { name: text('scr04.catalog.single_occupancy') }));
      await settle();
    });
    expect(calls).toBe(0);
    expect(write).not.toHaveBeenCalled();
  });
  it('retiro envía motivo/fecha y ancla último estándar; la acción permanece disponible', async () => {
    const { Wrapper } = await fixture();
    const sent: unknown[] = [];
    server.use(
      http.post(
        `http://localhost/zones/${ZONE}/standards/${STANDARD}/retirement`,
        async ({ request }) => {
          sent.push(await request.json());
          return HttpResponse.json(
            {
              code: 'invalid_request',
              detail_code: 'catalog_last_standard_in_zone',
              correlation_id: PLANT,
            },
            { status: 400 },
          );
        },
      ),
    );
    render(
      <Changes zoneId={ZONE} version={version()} standardId={STANDARD} timeZone="America/Bogota" />,
      {
        wrapper: Wrapper,
      },
    );
    fill('scr04.catalog.retirement_date', '2026-01-01T12:00');
    fill('scr04.catalog.reason', 'Motivo sintético suficiente');
    fireEvent.click(screen.getByRole('button', { name: text('scr04.catalog.retire') }));
    expect(
      await screen.findByText(label('catalog_detail_code', 'catalog_last_standard_in_zone')),
    ).toBeDefined();
    expect(sent).toEqual([
      { effective_from: '2026-01-01T17:00:00.000Z', reason_es: 'Motivo sintético suficiente' },
    ]);
    expect(
      screen.getByLabelText(text('scr04.catalog.retirement_date')).getAttribute('aria-invalid'),
    ).toBe('true');
    expect(
      screen.getByRole('button', { name: text('scr04.catalog.retire') }).hasAttribute('disabled'),
    ).toBe(false);
  });
  it.each([
    ['', 'America/Bogota'],
    ['2026-03-08T02:30', 'America/New_York'],
    ['2026-01-01T12:00', 'Zona/Inexistente'],
  ])('no envía un retiro con fecha no convertible %s en %s', async (date, timeZone) => {
    const { Wrapper } = await fixture();
    render(
      <Changes zoneId={ZONE} version={version()} standardId={STANDARD} timeZone={timeZone} />,
      { wrapper: Wrapper },
    );
    const write = vi.spyOn(catalogPort.retire, 'write');
    fill('scr04.catalog.retirement_date', date);
    fill('scr04.catalog.reason', 'Motivo sintético suficiente');
    await act(async () => {
      fireEvent.submit(screen.getByRole('form', { name: text('scr04.catalog.retire') }));
      await settle();
    });
    expect(write).not.toHaveBeenCalled();
    expect(
      screen.getByLabelText(text('scr04.catalog.retirement_date')).getAttribute('aria-invalid'),
    ).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe(text('validation.invalid'));
  });
  it('la página usa la zona horaria de planta al retirar', async () => {
    const { Wrapper } = await fixture();
    const write = vi.spyOn(catalogPort.retire, 'write').mockResolvedValue(version(2));
    render(<Page />, { wrapper: Wrapper });
    await screen.findByRole('heading', { name: 'Estándar sintético 1' });
    fill('scr04.catalog.retirement_date', '2026-01-01T07:00');
    // El reloj inyectado marca 12:00Z; 07:00 en Bogotá es ese mismo instante.
    fireEvent.change(
      screen.getAllByLabelText(
        `${text('scr04.catalog.reason')} (${text('components_forms.reason.required')})`,
      )[0] ?? document.body,
      { target: { value: 'Motivo sintético suficiente' } },
    );
    fireEvent.click(screen.getByRole('button', { name: text('scr04.catalog.retire') }));
    await waitFor(() => {
      expect(write).toHaveBeenCalledWith({
        path: { zone_id: ZONE, standard_id: STANDARD },
        body: {
          effective_from: '2026-01-01T12:00:00.000Z',
          reason_es: 'Motivo sintético suficiente',
        },
      });
    });
  });
  it('provider_installer lee catálogo y compara bajo la concesión del cliente', async () => {
    const { Wrapper } = await fixture(`/zones/${ZONE}/catalog`, {
      permissions: ['catalog.read'],
      concessionOrganizationId: CLIENT,
    });
    const calls: string[] = [];
    server.use(
      http.get(
        `http://localhost/zones/${ZONE}/catalog/versions/:version`,
        ({ params, request }) => {
          calls.push(request.headers.get('X-Vigia-Concession') ?? '');
          return HttpResponse.json({
            ...detail(Number(params.version)),
            catalog: { ...payload(Number(params.version)), organization_id: CLIENT },
          });
        },
      ),
    );
    render(<Page />, { wrapper: Wrapper });
    await screen.findByRole('heading', { name: 'Estándar sintético 1' });
    fill('scr04.catalog.from', '1');
    fill('scr04.catalog.to', '2');
    fireEvent.click(screen.getByRole('button', { name: text('scr04.catalog.compare') }));
    await screen.findByRole('heading', { name: text('scr04.catalog.changed') });
    expect(calls).toEqual([STANDARD, STANDARD]);
    expect(screen.queryByRole('button', { name: text('scr04.catalog.declare') })).toBeNull();
    expect(() =>
      catalogPayload(
        { ...version(), catalog: { ...payload(), organization_id: PLANT } },
        ZONE,
        ORG,
        CLIENT,
      ),
    ).toThrow('Catálogo de otra organización.');
  });
  it('una concesión no permite leer catálogos de un tercer cliente', async () => {
    const { Wrapper } = await fixture(`/zones/${ZONE}/catalog`, {
      permissions: ['catalog.read'],
      concessionOrganizationId: CLIENT,
    });
    server.use(
      http.get(`http://localhost/zones/${ZONE}/catalog`, () =>
        HttpResponse.json({ ...version(), catalog: { ...payload(), organization_id: PLANT } }),
      ),
    );
    render(<Page />, { wrapper: Wrapper });
    await screen.findByRole('table', { name: text('scr04.catalog.history') });
    expect(screen.queryByRole('heading', { name: 'Estándar sintético 1' })).toBeNull();
  });
  it('no muestra otra organización y admite la organización de una concesión', () => {
    const data = { ...payload(), organization_id: PLANT };
    const errors: unknown[] = [];
    const { rerender } = render(
      <CatalogView
        catalog={data}
        version={version()}
        timeZone="UTC"
        organizationId={ORG}
        reportContractError={(error) => {
          errors.push(error);
        }}
      />,
    );
    expect(screen.queryByRole('heading', { name: 'Estándar sintético 1' })).toBeNull();
    expect(errors).toEqual([{ code: 'foreign_organization' }]);
    rerender(
      <CatalogView
        catalog={data}
        version={version()}
        timeZone="UTC"
        organizationId={ORG}
        concessionOrganizationId={PLANT}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Estándar sintético 1' })).toBeDefined();
  });
});

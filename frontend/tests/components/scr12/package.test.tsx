/** Paquete, entrada, estados, descarga efímera y accesibilidad (IF-16, RV-16). */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import { downloadZone } from '@/ports/scr12Monthly';
import { text } from '@/texts';
import { PROPERTY_TIMEOUT_MS } from '../../support/timeouts';
import { assertNoPersonData } from '../../support/viewProperties';
import views from '../../views/scr12';
import { apiError, PERIOD, PLANT, setup, URL_ZONE, WAIT, ZONE } from './support';

function stringValues(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (value === null || typeof value !== 'object') return [];
  return Object.values(value).flatMap(stringValues);
}

describe('SCR-12 paquete mensual', { timeout: 60_000 }, () => {
  it('pide el paquete al abrir, muestra resumen honesto y no sondea ni precarga la muestra', async () => {
    const { calls, clock } = await setup();
    const summary = await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
    expect(
      within(summary)
        .getAllByRole('definition')
        .map((node) => node.textContent),
    ).toEqual(['8', '7', '5', '2', '3', '1', '1', '0']);
    expect(calls.reads).toEqual([`?zone_id=${ZONE}&period=${PERIOD}`]);
    expect(screen.getByText(text('scr12.exposure_unavailable')).textContent).toBe(
      text('scr12.exposure_unavailable'),
    );
    expect(screen.getByText(text('scr12.checkpoint_unavailable')).textContent).toBe(
      text('scr12.checkpoint_unavailable'),
    );
    await act(async () => {
      clock.advance(300_000);
      await Promise.resolve();
    });
    expect(calls.reads).toHaveLength(1);
    expect(calls.download).toBe(0);
  });

  it('409 sin sellar: muestra estado y no monta compromisos ni descarga', async () => {
    await setup({ packageResponse: () => apiError('accreditation_monthly_package_not_available') });
    await screen.findByText(text('scr12.unsealed'), {}, WAIT);
    expect(screen.queryByRole('form', { name: text('scr12.create') })).toBeNull();
    expect(screen.queryByRole('button', { name: text('scr12.download') })).toBeNull();
  });

  it('404 usa el mismo estado fuera de alcance y nunca muestra datos', async () => {
    await setup({ packageResponse: () => apiError(null, 404, 'not_found') });
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('region', { name: text('scr12.summary') })).toBeNull();
  });

  it('pide URL solo al pulsar, elige la zona y no guarda URL en DOM, caché ni storage', async () => {
    const local = vi.spyOn(Storage.prototype, 'setItem');
    const { calls, navigate, cache, view } = await setup();
    const button = await screen.findByRole('button', { name: text('scr12.download') }, WAIT);
    await waitFor(() => {
      expect(button).toHaveProperty('disabled', false);
    }, WAIT);
    expect(calls.download).toBe(0);
    fireEvent.click(button);
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledExactlyOnceWith(URL_ZONE);
    }, WAIT);
    expect(calls.download).toBe(1);
    expect(calls.downloadPaths).toEqual([
      `/accreditation/monthly-packages/${PLANT}/${PERIOD}/download`,
    ]);
    expect(cache.client.getMutationCache().getAll()).toHaveLength(0);
    for (const query of cache.client.getQueryCache().getAll()) {
      expect(query.queryKey[1]).not.toBe(
        'GET /accreditation/monthly-packages/{plant_id}/{period}/download',
      );
      expect(stringValues(query.state)).not.toContain(URL_ZONE);
    }
    expect(
      Array.from(view.container.querySelectorAll('*')).flatMap((element) =>
        Array.from(element.attributes).map((attribute) => attribute.value),
      ),
    ).not.toContain(URL_ZONE);
    expect(local).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => {
      expect(calls.download).toBe(2);
    }, WAIT);
  });

  it('no abre otra zona ni esquemas activos o credenciales en la URL', async () => {
    await setup({
      downloads: [{ zone_id: PLANT, url: URL_ZONE, expires_at: '2026-03-02T12:15:00.000Z' }],
    });
    const navigate = vi.fn();
    await expect(downloadZone(PLANT, PERIOD, ZONE, navigate)).rejects.toThrow();
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([
    ['java', 'script:alert(1)'].join(''),
    'http://objetos.example.test/paquete.zip',
    'https://usuario:clave@objetos.example.test/p.zip',
  ])('rechaza URL de descarga no navegable: %s', async (url) => {
    await setup({ downloads: [{ zone_id: ZONE, url, expires_at: '2026-03-02T12:15:00.000Z' }] });
    const navigate = vi.fn();
    await expect(downloadZone(PLANT, PERIOD, ZONE, navigate)).rejects.toThrow();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('la entrada aplica zona y mes solo al enviar el formulario', async () => {
    // Con dos zonas la entrada no abre sola el paquete: espera a que se envíe el formulario.
    const { calls } = await setup({ entry: true, twoZones: true });
    const zone = await screen.findByRole('combobox', { name: text('scr12.zone') }, WAIT);
    fireEvent.change(zone, { target: { value: ZONE } });
    const period = screen.getByLabelText(text('scr12.period'));
    expect(period.tagName).toBe('SELECT');
    fireEvent.change(period, { target: { value: PERIOD } });
    expect(calls.reads).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: text('scr12.open') }));
    await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
    expect(calls.reads).toEqual([`?zone_id=${ZONE}&period=${PERIOD}`]);
  });

  it('con una sola zona la entrada abre sola el paquete del mes anterior en la hora de la planta', async () => {
    // Reloj en 2026-03-02T12:00Z: el mes anterior en Bogotá es PERIOD (2026-02).
    const { calls, router } = await setup({ entry: true });
    await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
    expect(calls.reads).toEqual([`?zone_id=${ZONE}&period=${PERIOD}`]);
    expect(router.state.location.pathname).toBe(`/zones/${ZONE}/monthly/${PERIOD}`);
    expect(router.state.historyAction).toBe('REPLACE');
  });

  it('«Elegir otro mes o zona» vuelve a la entrada sin abrir de nuevo el paquete', async () => {
    const { calls, router } = await setup({ entry: true });
    await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
    fireEvent.click(screen.getByRole('link', { name: text('scr12.choose_again') }));
    const zone = await screen.findByRole<HTMLSelectElement>(
      'combobox',
      { name: text('scr12.zone') },
      WAIT,
    );
    expect(router.state.location.pathname).toBe('/zones/monthly');
    expect(router.state.location.state).toEqual({ choose: true });
    // La única zona y el mes anterior quedan elegidos, pero nada se pide hasta enviar.
    expect(zone.value).toBe(ZONE);
    expect(screen.getByLabelText<HTMLSelectElement>(text('scr12.period')).value).toBe(PERIOD);
    expect(calls.reads).toHaveLength(1);
  });

  it('axe sin violaciones serias o críticas en paquete y formularios', async () => {
    const { view } = await setup();
    await screen.findByRole('region', { name: text('scr12.summary') }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('scr12.load_sample') }));
    await screen.findByRole('form', { name: text('scr12.submit_result') }, WAIT);
    const result = await axe(view.container);
    expect(
      result.violations.filter((item) => item.impact === 'serious' || item.impact === 'critical'),
    ).toEqual([]);
  });

  // Una prueba por vista, cada una con su tope: con el perfil nightly (2 000 ejemplos) las tres
  // juntas pasaban de 180 s en el runner de GitHub (VIG-356).
  it.each(Object.keys(views))(
    'RV-16 rechaza campos de persona y campos ajenos con la vista de producción %s',
    (name) => {
      assertNoPersonData(views[name as keyof typeof views]);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('RV-16 cubre las tres vistas de producción', () => {
    expect(Object.keys(views)).toEqual(['package', 'commitment', 'sample']);
  });
});

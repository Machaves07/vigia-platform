/** Exposición sin desagregar personas, y siempre con cobertura del mismo periodo. */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router';
import Page from '@/pages/scr13/metrics';
import { ExposureView } from '@/pages/scr13/Views';
import { text } from '@/texts';
import { FakeClock } from '../../support/fakeClock';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  API,
  OTHER,
  WAIT,
  composite,
  coverageResponse,
  exposureData,
  fixture,
  consult,
} from './support';

describe('SCR-13 exposición', { timeout: 30_000 }, () => {
  it('cobertura solo a demanda, sin refresco al invalidar y sin cifras del periodo anterior', async () => {
    const f = await fixture();
    const requests: URL[] = [];
    server.use(
      http.get(`${API}/zones/:zone_id/coverage`, ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json(coverageResponse(f.organizationId));
      }),
    );
    const view = render(<Page />, { wrapper: f.Wrapper });
    await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
    expect(requests).toHaveLength(0);
    await consult();
    await screen.findByRole('heading', { name: text('coverage.timeline.title') }, WAIT);
    expect(requests).toHaveLength(1);
    expect(Object.fromEntries(requests[0]?.searchParams ?? [])).toEqual({
      from: '2026-01-01T00:00:00.000Z',
      to: '2026-02-01T00:00:00.000Z',
    });
    await act(async () => {
      f.clock.advance(300_000);
      await f.cache.invalidate({ screen: 'scr13', resources: ['coverage'] });
    });
    expect(requests).toHaveLength(1);
    fireEvent.change(screen.getByLabelText(text('scr13.period')), { target: { value: '2025-12' } });
    expect(view.container.querySelector('dd')).toBeNull();
    expect(requests).toHaveLength(1);
  });
  it.each([79.99, 80])(
    'tres cifras y cobertura inseparables, parcial solo bajo 80: %s',
    (percent) => {
      const data = { ...exposureData(), observable_coverage_percent: percent };
      const view = render(
        <ExposureView
          data={data}
          composite={composite()}
          clock={new FakeClock()}
          timeZone="UTC"
          onPeriodChange={() => undefined}
        />,
      );
      const exposure = screen.getByRole('region', { name: text('scr13.exposure') });
      expect(
        within(exposure).getByRole('heading', { name: text('coverage.timeline.title') }),
      ).toBeDefined();
      expect([...exposure.querySelectorAll('dd')].map((node) => node.textContent)).toEqual([
        '12',
        '7',
        String(percent),
      ]);
      expect(screen.queryByText(text('scr13.partial')) !== null).toBe(percent < 80);
      expect(screen.getByText('00:00–04:00', { exact: true })).toBeDefined();
      expect(view.container.querySelectorAll('tbody tr')).toHaveLength(7);
    },
  );
  it.each(['partial', 'not_computable'] as const)(
    'conserva aviso de uso y motivo de exposición %s',
    (status) => {
      const data = {
        ...exposureData(),
        status,
        partial_reason_es: 'Cobertura insuficiente del periodo.',
      };
      const view = render(
        <ExposureView
          data={data}
          composite={composite()}
          clock={new FakeClock()}
          timeZone="UTC"
          onPeriodChange={() => undefined}
        />,
      );
      expect(screen.getByText(data.usage_notice_es, { exact: true })).toBeDefined();
      expect(
        screen.getByText('Cobertura insuficiente del periodo.', { exact: true }),
      ).toBeDefined();
      expect(screen.getByText(text(`scr13.${status}`), { exact: true })).toBeDefined();
      if (status === 'not_computable') expect(view.container.querySelector('dd')).toBeNull();
    },
  );
  it('anotaciones en la zona horaria de la planta y fecha inválida explícita', () => {
    const data = exposureData();
    data.coverage_timeline.annotations = [
      {
        kind: 'gate_revoked',
        from: '2026-01-01T02:00:00.000Z',
        to: '2026-01-01T03:00:00.000Z',
        cause: null,
      },
      { kind: 'acceptance_not_current', from: '', to: '', cause: null },
    ];
    render(
      <ExposureView
        data={data}
        composite={composite()}
        clock={new FakeClock()}
        timeZone="America/Bogota"
        onPeriodChange={() => undefined}
      />,
    );
    const notes = screen.getByRole('region', { name: text('scr13.annotations') });
    expect(
      within(notes)
        .getAllByRole('listitem')
        .map((node) => node.textContent),
    ).toEqual([
      'Compuerta de uso revocada: 2025-12-31 21:00:00 -05:00–2025-12-31 22:00:00 -05:00',
      'Acta no vigente: Fecha no disponible–Fecha no disponible',
    ]);
  });
  it.each(['incomplete', 'wrong_period', 'wrong_embedded_period'] as const)(
    'no muestra cifras con cobertura %s',
    (kind) => {
      const data = exposureData();
      const coverage = composite();
      if (kind === 'wrong_embedded_period')
        data.coverage_timeline.period.from = '2025-12-01T00:00:00.000Z';
      const invalid =
        kind === 'incomplete'
          ? { ...coverage, status: 'incomplete' as const, problems: [] }
          : kind === 'wrong_period'
            ? { ...coverage, period: { ...coverage.period, fromMs: coverage.period.fromMs - 1 } }
            : coverage;
      const view = render(
        <MemoryRouter>
          <ExposureView
            data={data}
            composite={invalid}
            clock={new FakeClock()}
            timeZone="UTC"
            onPeriodChange={() => undefined}
          />
        </MemoryRouter>,
      );
      expect(view.container.querySelector('dd')).toBeNull();
      expect(screen.getByText(text('screen_state.unavailable'))).toBeDefined();
    },
  );
  it.each([
    'accreditation_coverage_unavailable',
    'loop_gate_state_unavailable',
    'accreditation_exposure_not_computable',
  ])('degrada solo exposición por %s', async (code) => {
    const f = await fixture();
    server.use(
      http.get(`${API}/accreditation/exposure`, () =>
        apiError(
          code === 'accreditation_exposure_not_computable'
            ? 'invalid_request'
            : 'temporarily_unavailable',
          code === 'accreditation_exposure_not_computable' ? 400 : 503,
          { detailCode: code },
        ),
      ),
    );
    const view = render(<Page />, { wrapper: f.Wrapper });
    await consult();
    await screen.findByRole('heading', { name: text('scr13.panel_usage') }, WAIT);
    const section = view.container.querySelector<HTMLElement>('[data-section="exposure"]');
    if (section === null) throw new Error('Falta la sección.');
    await within(section).findByText(
      code === 'accreditation_exposure_not_computable'
        ? text('scr13.not_computable')
        : text('screen_state.unavailable'),
      {},
      WAIT,
    );
    expect(view.container.querySelector('dd')).toBeNull();
    expect(screen.getByRole('heading', { name: text('scr13.activation') })).toBeDefined();
  });
  it.each(['zone', 'period', 'from', 'to'] as const)(
    'rechaza exposición con otro %s',
    async (field) => {
      const f = await fixture();
      const data = exposureData();
      if (field === 'zone') data.zone_id = OTHER;
      if (field === 'period') data.period = '2025-12';
      if (field === 'from') data.period_from = '2025-12-01T00:00:00.000Z';
      if (field === 'to') data.period_to = '2026-03-01T00:00:00.000Z';
      server.use(http.get(`${API}/accreditation/exposure`, () => HttpResponse.json(data)));
      const view = render(<Page />, { wrapper: f.Wrapper });
      await consult();
      await screen.findByRole('alert', {}, WAIT);
      expect(view.container.querySelector('dd')).toBeNull();
    },
  );
  it.each(['organization', 'plant', 'zone'] as const)(
    'no muestra exposición con cobertura de otra %s',
    async (field) => {
      const f = await fixture();
      const data = coverageResponse(f.organizationId);
      if (field === 'organization') data.organization_id = OTHER;
      if (field === 'plant') data.plant_id = OTHER;
      if (field === 'zone') data.zone_id = OTHER;
      server.use(http.get(`${API}/zones/:zone_id/coverage`, () => HttpResponse.json(data)));
      const view = render(<Page />, { wrapper: f.Wrapper });
      await consult();
      await screen.findByText(text('screen_state.unavailable'), {}, WAIT);
      expect(view.container.querySelector('dd')).toBeNull();
    },
  );
});

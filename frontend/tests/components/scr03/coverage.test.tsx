/** RV-06: consulta explícita por zona/periodo, composición compartida y aislamiento. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr03/coverage';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { apiError } from '../../msw/handlers';
import { API, OTHER_ZONE, PLANT, WAIT, ZONE, coverageResponse, integrityFixture } from './support';

describe('SCR-03 cobertura', { timeout: 30_000 }, () => {
  it('un not_found muestra el estado común sin fingir cobertura', async () => {
    const fixture = await integrityFixture(`/zones/${ZONE}/coverage`);
    server.use(http.get(`${API}/zones/:zone_id/coverage`, () => apiError('not_found', 404)));
    render(<Page />, { wrapper: fixture.Wrapper });
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('table', { name: text('coverage.summary.title') })).toBeNull();
  });
  it('no lee al abrir; pide siete días al pulsar y doce meses en tramos de hasta 31, sin sondeos', async () => {
    const fixture = await integrityFixture(`/zones/${ZONE}/coverage`, true);
    const requests: URL[] = [];
    server.use(
      http.get(`${API}/zones/:zone_id/coverage`, ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json(coverageResponse(request, fixture.organizationId));
      }),
    );
    const view = render(<Page />, { wrapper: fixture.Wrapper });
    await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
    expect(requests).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await screen.findByRole('table', { name: text('coverage.summary.title') }, WAIT);
    expect(requests).toHaveLength(1);
    const first = requests[0];
    if (first === undefined) throw new Error('Falta la consulta.');
    expect(
      Date.parse(first.searchParams.get('to') ?? '') -
        Date.parse(first.searchParams.get('from') ?? ''),
    ).toBe(7 * 86_400_000);
    fireEvent.change(screen.getByLabelText(text('coverage.period.label')), {
      target: { value: 'last_12_months' },
    });
    await waitFor(() => {
      expect(requests).toHaveLength(13);
      expect(screen.getByRole('table', { name: text('coverage.summary.title') })).toBeDefined();
    }, WAIT);
    const chunks = requests.slice(1);
    for (const [index, url] of chunks.entries()) {
      const span =
        Date.parse(url.searchParams.get('to') ?? '') -
        Date.parse(url.searchParams.get('from') ?? '');
      expect(span).toBeGreaterThan(0);
      expect(span).toBeLessThanOrEqual(31 * 86_400_000);
      if (index > 0)
        expect(url.searchParams.get('from')).toBe(chunks[index - 1]?.searchParams.get('to'));
      expect(url.pathname).toBe(`/zones/${ZONE}/coverage`);
    }
    expect(await axe(view.container)).toHaveNoViolations();
    await act(async () => {
      fixture.clock.advance(300_000);
      await fixture.cache.invalidate({ screen: 'scr03', resources: ['coverage'] });
    });
    fireEvent.focus(window);
    fireEvent(window, new Event('online'));
    expect(requests).toHaveLength(13);
    fireEvent.change(screen.getByLabelText(text('scr03.zone')), { target: { value: OTHER_ZONE } });
    await waitFor(() => {
      expect(
        screen.getByLabelText(text('scr03.zone')).getAttribute('value') ??
          screen.getByLabelText<HTMLSelectElement>(text('scr03.zone')).value,
      ).toBe(OTHER_ZONE);
    }, WAIT);
    expect(requests).toHaveLength(13);
    expect(screen.queryByRole('table', { name: text('coverage.summary.title') })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: text('components_status.refresh.button') }));
    await screen.findByRole('table', { name: text('coverage.summary.title') }, WAIT);
    expect(requests.at(-1)?.pathname).toBe(`/zones/${OTHER_ZONE}/coverage`);
    expect(requests).toHaveLength(14);
  });

  it.each(['organization', 'plant', 'zone'] as const)(
    'no muestra resumen de otra %s',
    async (field) => {
      const fixture = await integrityFixture(`/zones/${ZONE}/coverage`);
      server.use(
        http.get(`${API}/zones/:zone_id/coverage`, ({ request }) =>
          HttpResponse.json(
            coverageResponse(
              request,
              fixture.organizationId,
              field === 'organization'
                ? { organization_id: OTHER_ZONE }
                : field === 'plant'
                  ? { plant_id: OTHER_ZONE }
                  : { zone_id: PLANT },
            ),
          ),
        ),
      );
      render(<Page />, { wrapper: fixture.Wrapper });
      await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
      fireEvent.click(
        screen.getByRole('button', { name: text('components_status.refresh.button') }),
      );
      await screen.findByText(text('coverage.incomplete.missing_chunk'), {}, WAIT);
      expect(screen.queryByRole('table', { name: text('coverage.summary.title') })).toBeNull();
    },
  );
});

/** IF-18: acuse asíncrono y lecturas exclusivamente explícitas. */
import { readFileSync } from 'node:fs';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr03/integrity';
import { integrityPort, verifierDownload } from '@/ports/scr03Integrity';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { apiError } from '../../msw/handlers';
import { API, INSTANT, PLANT, WAIT, integrityFixture } from './support';

function region(key: string) {
  return screen.getByRole('region', { name: text(`scr03.${key}`) });
}
function refresh(key: string) {
  fireEvent.click(
    within(region(key)).getByRole('button', { name: text('components_status.refresh.button') }),
  );
}

describe('SCR-03 integridad', { timeout: 30_000 }, () => {
  it('verifica la cadena elegida, muestra ruptura con fecha y hash recibido, sin refrescar en cinco minutos', async () => {
    const fixture = await integrityFixture('/integrity', true);
    const calls = { verify: 0, results: 0, checkpoints: 0, keys: 0, verifier: 0 };
    const bodies: unknown[] = [];
    const hash = 'hash-publicado-sintetico-no-calculado';
    server.use(
      http.post(`${API}/integrity/verify`, async ({ request }) => {
        calls.verify++;
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            request_id: PLANT,
            chain: { kind: 'ledger', plant_id: PLANT },
            status: 'queued',
            requested_at: INSTANT,
          },
          { status: 202 },
        );
      }),
      http.get(`${API}/integrity/results`, () => {
        calls.results++;
        return HttpResponse.json({
          results: [
            {
              chain: { kind: 'ledger', plant_id: PLANT },
              mode: 'full',
              result: 'broken',
              from_sequence: 1,
              to_sequence: 25,
              head_sequence: 25,
              broken_sequence: 17,
              broken_entry_id: PLANT,
              reason: 'hash_mismatch',
              verified_at: INSTANT,
            },
          ],
        });
      }),
      http.get(`${API}/integrity/checkpoints`, () => {
        calls.checkpoints++;
        return HttpResponse.json({
          organization_id: fixture.organizationId,
          checkpoints: [
            {
              chain: { kind: 'ledger', plant_id: PLANT },
              sequence: 25,
              taken_at: INSTANT,
              key_id: 'clave-sintetica',
              signature: 'firma-sintetica',
            },
          ],
        });
      }),
      http.get(`${API}/.well-known/vigia-checkpoint-keys`, () => {
        calls.keys++;
        return HttpResponse.json({
          purpose: 'checkpoint',
          keys: [{ key_id: 'clave-sintetica', public_key: 'publica-sintetica' }],
        });
      }),
      http.get(`${API}/.well-known/vigia-verifier`, () => {
        calls.verifier++;
        return HttpResponse.json({
          name: 'vigia_verify.py',
          sha256: hash,
          size_bytes: 100,
          format_version: 1,
        });
      }),
    );
    const view = render(<Page />, { wrapper: fixture.Wrapper });
    await screen.findByRole('option', { name: 'Planta sintética' }, WAIT);
    expect(calls).toEqual({ verify: 0, results: 0, checkpoints: 0, keys: 0, verifier: 0 });
    fireEvent.change(screen.getByLabelText(text('scr03.chain')), { target: { value: PLANT } });
    fireEvent.click(screen.getByRole('button', { name: text('scr03.verify') }));
    await screen.findByText(text('scr03.queued'), { exact: false }, WAIT);
    expect(bodies).toEqual([{ kind: 'ledger', plant_id: PLANT }]);
    expect(calls.results).toBe(0);
    refresh('history');
    refresh('checkpoints');
    refresh('verifier');
    await screen.findByText('Rota en la secuencia 17', {}, WAIT);
    await screen.findByText('publica-sintetica', {}, WAIT);
    await screen.findByText(hash, {}, WAIT);
    expect(region('history').querySelector(`time[datetime="${INSTANT}"]`)).not.toBeNull();
    const link = screen.getByRole('link', { name: text('scr03.download') });
    expect(link.getAttribute('href')).toBe(verifierDownload.href);
    expect(link.getAttribute('download')).toBe('vigia_verify.py');
    expect(view.container.querySelector('input[type=file]')).toBeNull();
    expect(await axe(view.container)).toHaveNoViolations();
    await act(async () => {
      fixture.clock.advance(300_000);
      await fixture.cache.invalidate({ screen: 'scr03', resources: ['integrity'] });
    });
    fireEvent.focus(window);
    fireEvent(window, new Event('online'));
    expect(calls).toEqual({ verify: 1, results: 1, checkpoints: 1, keys: 1, verifier: 1 });
    view.unmount();
    render(<Page />, { wrapper: fixture.Wrapper });
    act(() => {
      fixture.clock.advance(300_000);
    });
    expect(calls).toEqual({ verify: 1, results: 1, checkpoints: 1, keys: 1, verifier: 1 });
  });

  it('declara solo operaciones fixed y el hash no se calcula con criptografía del navegador', () => {
    expect(integrityPort.operations.every((operation) => operation.status === 'fixed')).toBe(true);
    const source = [
      'src/pages/scr03/integrity/index.tsx',
      'src/pages/scr03/IntegrityViews.tsx',
      'src/ports/scr03Integrity.ts',
    ]
      .map((path) => readFileSync(path, 'utf8'))
      .join('\n');
    expect(source).not.toMatch(/crypto|digest|sha256\(/);
  });

  it.each(['ledger', 'audit'])(
    'envía la cadena de organización %s, sin planta ni consulta automática',
    async (kind) => {
      const fixture = await integrityFixture();
      const bodies: unknown[] = [];
      server.use(
        http.post(`${API}/integrity/verify`, async ({ request }) => {
          bodies.push(await request.json());
          return HttpResponse.json(
            {
              request_id: PLANT,
              chain: { kind, plant_id: null },
              status: 'queued',
              requested_at: INSTANT,
            },
            { status: 202 },
          );
        }),
      );
      render(<Page />, { wrapper: fixture.Wrapper });
      fireEvent.change(screen.getByLabelText(text('scr03.chain')), { target: { value: kind } });
      fireEvent.click(screen.getByRole('button', { name: text('scr03.verify') }));
      await screen.findByText(text('scr03.queued'), { exact: false }, WAIT);
      expect(bodies).toEqual([{ kind }]);
    },
  );

  it('muestra continua, fecha ausente y resultado desconocido sin reinterpretarlos', async () => {
    const fixture = await integrityFixture();
    server.use(
      http.get(`${API}/integrity/results`, () =>
        HttpResponse.json({
          results: [
            {
              chain: { kind: 'audit', plant_id: null },
              head_sequence: 9,
              result: 'intact',
              broken_sequence: null,
              verified_at: null,
            },
            {
              chain: { kind: 'ledger', plant_id: null },
              head_sequence: 1,
              result: 'pending-future',
              broken_sequence: null,
              verified_at: INSTANT,
            },
          ],
        }),
      ),
    );
    render(<Page />, { wrapper: fixture.Wrapper });
    refresh('history');
    await screen.findByText(text('scr03.intact'), {}, WAIT);
    expect(screen.getByText(text('scr03.no_date'))).toBeDefined();
    expect(screen.getByText(text('scr03.unknown'))).toBeDefined();
  });

  it('rechazo de escritura no se reintenta ni produce un resultado de verificación ficticio', async () => {
    const fixture = await integrityFixture();
    let writes = 0;
    server.use(
      http.post(`${API}/integrity/verify`, () => {
        writes++;
        return apiError('not_found', 404);
      }),
    );
    render(<Page />, { wrapper: fixture.Wrapper });
    fireEvent.click(screen.getByRole('button', { name: text('scr03.verify') }));
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    act(() => {
      fixture.clock.advance(300_000);
    });
    expect(writes).toBe(1);
    expect(screen.queryByText(text('scr03.queued'), { exact: false })).toBeNull();
  });

  it('muestra el mismo estado común para not_found sin conservar resultados anteriores', async () => {
    server.use(http.get(`${API}/integrity/results`, () => apiError('not_found', 404)));
    const fixture = await integrityFixture();
    render(<Page />, { wrapper: fixture.Wrapper });
    refresh('history');
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('table', { name: text('scr03.history') })).toBeNull();
  });
});

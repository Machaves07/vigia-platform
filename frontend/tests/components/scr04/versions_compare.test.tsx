import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { Versions } from '@/pages/scr04/catalog/Versions';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { detail, fixture, fill, ZONE } from './support';

describe('SCR-04 comparación', { timeout: 30_000 }, () => {
  it('pide exactamente dos versiones, ninguna ruta diff y muestra títulos cambiados', async () => {
    const { Wrapper } = await fixture();
    const calls: string[] = [];
    server.use(
      http.get(
        `http://localhost/zones/${ZONE}/catalog/versions/:version`,
        ({ params, request }) => {
          calls.push(new URL(request.url).pathname);
          return HttpResponse.json(detail(Number(params.version)));
        },
      ),
    );
    render(<Versions zoneId={ZONE} timeZone="America/Bogota" />, { wrapper: Wrapper });
    fill('scr04.catalog.from', '1');
    fill('scr04.catalog.to', '2');
    fireEvent.click(screen.getByRole('button', { name: text('scr04.catalog.compare') }));
    expect(await screen.findByText('Estándar sintético 1')).toBeDefined();
    expect(screen.getByText('Estándar sintético 2')).toBeDefined();
    expect(calls).toEqual([
      `/zones/${ZONE}/catalog/versions/1`,
      `/zones/${ZONE}/catalog/versions/2`,
    ]);
    expect(screen.getByText(`${text('scr04.catalog.signed')} 1`)).toBeDefined();
    expect(screen.getByText(`${text('scr04.catalog.signed')} 2`)).toBeDefined();
  });
  it('no solicita la misma versión dos veces y respeta cursores opacos', async () => {
    const { Wrapper } = await fixture();
    const cursors: (string | null)[] = [];
    server.use(
      http.get(`http://localhost/zones/${ZONE}/catalog/versions`, ({ request }) => {
        const after = new URL(request.url).searchParams.get('after');
        cursors.push(after);
        return HttpResponse.json({
          versions: [detail(after === null ? 1 : 2)],
          next_after: after === null ? 'opaco-sintetico' : null,
        });
      }),
    );
    render(<Versions zoneId={ZONE} timeZone="America/Bogota" />, { wrapper: Wrapper });
    fireEvent.click(
      await screen.findByRole('button', { name: text('components_structure.keyset.load_more') }),
    );
    await waitFor(() => {
      expect(cursors).toEqual([null, 'opaco-sintetico']);
    });
    fill('scr04.catalog.from', '1');
    fill('scr04.catalog.to', '1');
    fireEvent.click(screen.getByRole('button', { name: text('scr04.catalog.compare') }));
    expect(screen.queryByRole('heading', { name: text('scr04.catalog.changed') })).toBeNull();
  });
});

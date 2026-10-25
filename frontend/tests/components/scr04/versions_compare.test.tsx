import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { Versions } from '@/pages/scr04/catalog/Versions';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { ComparisonView } from '@/pages/scr04/catalog/ComparisonView';
import registration from '../../views/scr04';
import {
  assertNoPersonData,
  assertOrganizationIsolation,
  forbiddenRendered,
} from '../../support/viewProperties';
import { detail, fixture, fill, ORG, payload, ZONE } from './support';

describe('SCR-04 comparación', { timeout: 30_000 }, () => {
  it('RV-07 comparación: propiedades de privacidad y organización sobre la vista real', () => {
    assertNoPersonData(registration.comparison);
    expect(assertOrganizationIsolation(registration.comparison)).toBeGreaterThan(0);
  }, 180_000);
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
    const table = await screen.findByRole('table', { name: text('scr04.catalog.compare') });
    expect(
      within(table)
        .getAllByRole('row')
        .map((row) => [...row.children].map((cell) => cell.textContent)),
    ).toEqual([
      [text('scr04.catalog.changed'), text('scr04.catalog.before'), text('scr04.catalog.after')],
      [
        `${text('scr04.catalog.standard')} · Estándar sintético 2 · ${text('scr04.catalog.title_es')}`,
        'Estándar sintético 1',
        'Estándar sintético 2',
      ],
      [
        `${text('scr04.catalog.standard')} · Estándar sintético 2 · ${text('scr04.catalog.version')}`,
        '1',
        '2',
      ],
    ]);
    expect(
      within(table).queryByRole('rowheader', { name: text('scr04.catalog.review') }),
    ).toBeNull();
    expect(calls).toEqual([
      `/zones/${ZONE}/catalog/versions/1`,
      `/zones/${ZONE}/catalog/versions/2`,
    ]);
    expect(screen.queryByText(`${text('scr04.catalog.signed')} 1`)).toBeNull();
  });
  it('omite cámaras, señales, episodio y metadatos del sobre aunque cambien', () => {
    const before = { ...detail(1), catalog: payload(1) };
    const after = {
      ...detail(1),
      catalog: payload(1),
      issued_by: 'autor-fuera-de-rv07',
      envelope: { ...detail().envelope, key_id: 'clave-fuera-de-rv07' },
    };
    after.catalog.cameras = after.catalog.cameras.map((camera) => ({
      ...camera,
      code: 'CAM-SINT-OTRA',
    }));
    after.catalog.episode.grouping_window_ms = 123456;
    after.catalog.signals = [
      {
        signal_id: ZONE,
        code: 'senal-sintetica',
        role: 'energy',
        asserted_level: 'high',
        source: { reader: 'lector-sintetico', channel: 123 },
        description_es: 'descripcion-fuera-de-rv07',
      },
    ];
    const { container } = render(
      <ComparisonView before={before} after={after} organizationId={ORG} />,
    );
    expect(screen.getByText(text('scr04.catalog.no_changes'))).toBeDefined();
    expect(screen.queryAllByRole('rowheader')).toEqual([]);
    expect(forbiddenRendered(registration.comparison, { before, after }, container)).toEqual([]);
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

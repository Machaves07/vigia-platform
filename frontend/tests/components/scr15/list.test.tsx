/** Lista propia: cursor, destinos, acciones unitarias, estados y árbol accesible. */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { MemoryRouter, matchPath } from 'react-router';
import { SCREENS } from '@/app/screens';
import { NotificationView, notificationDestination } from '@/pages/scr15/NotificationView';
import { text } from '@/texts';
import { settle } from '../../support/queryCache';
import { CURSOR, ID, INSTANT, SECOND, WAIT, apiError, notification, setup } from './support';

describe('SCR-15 lista', { timeout: 60_000 }, () => {
  it('sin acción todas: un clic produce exactamente un POST y actualiza la campana', async () => {
    const { calls, view } = await setup({
      withBell: true,
      items: [notification(), notification(SECOND)],
    });
    const rows = await screen.findAllByRole('listitem', {}, WAIT);
    expect(rows).toHaveLength(2);
    const [untouched, clicked] = rows;
    if (untouched === undefined || clicked === undefined) throw new Error('Faltan los dos avisos');
    const clickedRow = within(clicked);
    const untouchedRow = within(untouched);
    const button = clickedRow.getByRole('button', { name: text('scr15.mark_read') });
    await screen.findByText(`${text('scr15.unread')}: 2`, {}, WAIT);
    // Una sola actualización por página (VIG-364): la campana no repite «Actualizar» mientras
    // tiene recuento; solo la lista lo ofrece.
    expect(screen.getAllByRole('button').map((node) => node.textContent)).toEqual([
      text('components_status.refresh.button'),
      text('scr15.mark_read'),
      text('scr15.mark_read'),
    ]);
    fireEvent.click(button);
    await waitFor(() => {
      expect(calls.marks).toEqual([SECOND]);
      expect(clickedRow.queryByRole('button', { name: text('scr15.mark_read') })).toBeNull();
      expect(untouchedRow.getByRole('button', { name: text('scr15.mark_read') })).toHaveProperty(
        'disabled',
        false,
      );
    }, WAIT);
    await screen.findByText(`${text('scr15.unread')}: 1`, {}, WAIT);
    expect(
      (await axe(view.container)).violations.filter(
        (v) => v.impact === 'serious' || v.impact === 'critical',
      ),
    ).toEqual([]);
  });

  it('no reintenta un POST fallido; conserva el aviso y permite reintento explícito', async () => {
    const { calls } = await setup({ markResponse: () => apiError(400, 'invalid_request') });
    fireEvent.click(await screen.findByRole('button', { name: text('scr15.mark_read') }, WAIT));
    await screen.findByRole('alert', {}, WAIT);
    expect(calls.marks).toEqual([ID]);
    expect(screen.getByRole('button', { name: text('scr15.mark_read') })).toHaveProperty(
      'disabled',
      false,
    );
  });

  it('deshabilita la acción mientras espera la respuesta', async () => {
    let resolve!: (response: Response) => void;
    const pending = new Promise<Response>((done) => {
      resolve = done;
    });
    const { calls } = await setup({ markResponse: () => pending });
    const button = await screen.findByRole('button', { name: text('scr15.mark_read') }, WAIT);
    fireEvent.click(button);
    await waitFor(() => {
      expect(button).toHaveProperty('disabled', true);
    }, WAIT);
    fireEvent.click(button);
    await act(async () => {
      resolve(new HttpResponse(null, { status: 204 }));
      await settle();
    });
    expect(calls.marks).toEqual([ID]);
  });

  it('cargar más usa next_after como after, acumula páginas y refresca sin duplicados', async () => {
    const { calls, clock } = await setup({ more: [notification(SECOND)] });
    fireEvent.click(await screen.findByRole('button', { name: text('scr15.load_more') }, WAIT));
    await waitFor(() => {
      expect(screen.getAllByRole('listitem')).toHaveLength(2);
    }, WAIT);
    expect(calls.reads.map((url) => url.searchParams.get('after'))).toEqual([null, null, CURSOR]);
    expect(screen.queryByRole('button', { name: text('scr15.load_more') })).toBeNull();
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    await waitFor(() => {
      expect(calls.reads).toHaveLength(5);
    }, WAIT);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('no duplica filas si las páginas se solapan por una llegada concurrente', async () => {
    await setup({ more: [notification()] });
    fireEvent.click(await screen.findByRole('button', { name: text('scr15.load_more') }, WAIT));
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: text('scr15.load_more') })).toBeNull();
      expect(screen.getAllByRole('listitem')).toHaveLength(1);
    }, WAIT);
  });

  it('abre la pantalla origen y llega al formulario mediante navegación accesible', async () => {
    const { router } = await setup();
    fireEvent.click(await screen.findByRole('link', { name: text('scr15.open_origin') }, WAIT));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/findings/${ID}`);
    }, WAIT);
    await act(async () => {
      await router.navigate('/notifications');
    });
    fireEvent.click(await screen.findByRole('link', { name: text('scr15.preferences') }, WAIT));
    await screen.findByRole('form', { name: text('scr15.preferences') }, WAIT);
  });

  it.each([
    ['finding', `/findings/${ID}`],
    ['queue_item', `/review-queue?item_id=${ID}`],
    ['package', `/zones/monthly?package_id=${ID}`],
    ['export', `/exports?export_id=${ID}`],
    ['node', `/fleet/nodes/${ID}`],
    ['organization', '/admin/organization'],
  ])('resuelve el destino interno %s', (kind, expected) => {
    expect(notificationDestination({ ...notification(), deep_link_ref: { kind, id: ID } })).toBe(
      expected,
    );
    const pathname = new URL(expected, 'http://localhost').pathname;
    expect(
      SCREENS.flatMap((s) => s.route_groups.flatMap((g) => g.paths)).some(
        (path) => matchPath({ path, end: true }, pathname) !== null,
      ),
    ).toBe(true);
  });

  it('los avisos de acción abren el lazo del hallazgo, no el detalle de lectura', () => {
    expect(notificationDestination({ ...notification(), event_name: 'action_assigned' })).toBe(
      `/findings/${ID}/loop`,
    );
  });

  it('sin nombre ni destino común de zona, no muestra UUID ni enlaza a transparencia', () => {
    const item = { ...notification(), deep_link_ref: { kind: 'zone', id: ID } };
    expect(notificationDestination(item)).toBeNull();
    const view = render(
      <MemoryRouter>
        <NotificationView item={item} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link')).toBeNull();
    expect(view.container.querySelectorAll('dd')[2]?.textContent).toBe(
      text('scr15.zone_unavailable'),
    );
    expect(screen.getByText(text('scr15.origin_unavailable'))).not.toBeNull();
  });

  it('no convierte kinds desconocidos en URL ni muestra cuerpo, imágenes o campos extra', () => {
    const item = {
      ...notification(),
      deep_link_ref: { kind: 'https://externo.example.test', id: ID },
      image_url: 'https://externo.example.test/foto',
      person_name: 'Identidad sintética prohibida',
    };
    const view = render(
      <MemoryRouter>
        <NotificationView item={item} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.queryByText(item.body_es)).toBeNull();
    expect(screen.queryByText(item.person_name)).toBeNull();
    expect(view.container.querySelectorAll('dd')[2]?.textContent).toBe(
      text('scr15.zone_unavailable'),
    );
  });

  it('no ofrece marcar una notificación ya leída', async () => {
    await setup({ items: [{ ...notification(), read_at: INSTANT }] });
    await screen.findByText(text('scr15.read'), {}, WAIT);
    expect(screen.queryByRole('button', { name: text('scr15.mark_read') })).toBeNull();
  });

  it('vacío y fuera de alcance son estados comunes, sin mostrar datos antiguos', async () => {
    await setup({ items: [] });
    await screen.findByText(text('scr15.empty'), {}, WAIT);
  });
  it('404 usa el estado uniforme de alcance', async () => {
    await setup({ listResponse: () => apiError() });
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('list')).toBeNull();
  });
});

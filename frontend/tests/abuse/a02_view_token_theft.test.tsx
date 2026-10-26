import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import { installTransport } from '@/ports/base';
import { text } from '@/texts';
import { server } from '../msw/server';
import { handoff, liveViewHarness, TOKEN, tokenFoundIn, ZONE_ID } from '../support/liveView';

afterEach(() => {
  installTransport(null);
});

it('A-2 · BR-APP-65, 66: abrir descarta el token sin historial, almacenamiento ni registros de la aplicación', async () => {
  const env = liveViewHarness();
  server.use(env.granted(handoff()));
  const push = vi.spyOn(history, 'pushState');
  const replace = vi.spyOn(history, 'replaceState');
  const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  render(<LiveViewLauncher zone_id={ZONE_ID} clock={env.clock} />);
  fireEvent.click(screen.getByRole('button', { name: text('live_view.open') }));
  const link = await screen.findByRole<HTMLAnchorElement>(
    'link',
    { name: text('live_view.link') },
    { timeout: 10_000 },
  );
  expect(new URL(link.href).search).toBe('');
  expect(new URL(link.href).hash).toBe(`#token=${TOKEN}`);
  expect(link.rel).toBe('noopener noreferrer');
  fireEvent.click(link);
  expect(tokenFoundIn(TOKEN)).toEqual([]);
  expect(push).not.toHaveBeenCalled();
  expect(replace).not.toHaveBeenCalled();
  expect(
    JSON.stringify([env.requests, env.devLog, log.mock.calls, warn.mock.calls, error.mock.calls]),
  ).not.toContain(TOKEN);
  expect(screen.queryByRole('link', { name: text('live_view.link') })).toBeNull();
  // D-1: limpiar el fragmento en la pestaña del nodo corresponde a U-06, no a esta SPA.
}, 30_000);

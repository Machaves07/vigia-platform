/** Regresión de integración VIG-302/VIG-304: la vista en vivo presenta el bloqueo sin red.
 * PR-APP-25: versión distinta impide la escritura y no dispara recarga.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import { createAppVersion } from '@/app/version';
import { LiveViewLauncher } from '@/components/LiveViewLauncher/LiveViewLauncher';
import { createPortConnection, createTransport, installTransport, PortError } from '@/ports/base';
import { behaviourFor } from '@/ports/errors/map';
import { liveViewFailure } from '@/ports/liveview';

import { FakeClock } from '../../support/fakeClock';

it('vista en vivo obsoleta muestra el aviso de versión, no uno interno ni un reintento', async () => {
  expect(
    liveViewFailure(
      new PortError({ kind: 'version_outdated', message: 'Hay una versión nueva; recargue.' }),
    ),
  ).toEqual({ code: 'version_outdated', status: 0 });
  expect(behaviourFor({ code: 'version_outdated', status: 0 })).toEqual({
    kind: 'forbidden_reload',
    text_key: 'version.outdated',
    message_es: 'Hay una versión nueva; recargue.',
  });
  const clock = new FakeClock();
  const reload = vi.fn();
  const fetch = vi.fn<typeof globalThis.fetch>();
  const version = createAppVersion({ clock, reload });
  version.observeApiVersion('next-release');
  installTransport(
    createTransport({ clock, connection: createPortConnection(clock), version, fetch }),
  );
  try {
    render(<LiveViewLauncher zone_id="0190a1b2-0000-7000-8000-0000000000a1" clock={clock} />);
    fireEvent.click(screen.getByRole('button', { name: 'Abrir vista en vivo' }));
    const notice = await screen.findByText('Hay una versión nueva; recargue.', undefined, {
      timeout: 10_000,
    });
    expect(notice.closest('[data-code]')?.getAttribute('data-code')).toBe('version_outdated');
    expect(screen.queryByRole('button', { name: 'Repetir' })).toBeNull();
    expect(screen.queryByText('Pidiendo el enlace…')).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  } finally {
    installTransport(null);
  }
});

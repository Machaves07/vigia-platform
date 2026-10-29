/**
 * VIG-365: el mando de línea y el COPASST tienen un inicio propio y corto. Como inicio de la sesión
 * la página se llama «Inicio», dice para qué sirve y ofrece sus accesos (solo los que la sesión
 * puede abrir); desde el menú de otro rol conserva su nombre de página. Las aserciones comparan
 * claves del catálogo y atributos exactos, nunca subcadenas con datos (VIG-194). Datos sintéticos.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Copasst from '@/pages/scr16/entry';
import { text } from '@/texts';
import { setup } from './scr12/support';
import { transparencyFixture } from './scr16/support';

const WAIT = { timeout: 20_000 };

function cards(container: HTMLElement) {
  return [...container.querySelectorAll('[data-slot]')].map((card) => [
    card.getAttribute('data-slot'),
    card.querySelector('a')?.getAttribute('href'),
  ]);
}

describe('inicio del mando de línea (SCR-12)', { timeout: 40_000 }, () => {
  it('como su inicio: «Inicio», para qué sirve y la transparencia de sus zonas', async () => {
    const { view } = await setup({
      entry: true,
      // Con una sola zona la entrada abriría sola el paquete (VIG-366); aquí se mira la entrada.
      twoZones: true,
      role: 'line_manager',
      permissions: ['review_package.read', 'commitments.write', 'transparency.read'],
    });
    await screen.findByRole('heading', { level: 1, name: text('scr12.home.title') }, WAIT);
    expect(screen.getByText(text('scr12.home.lead'), { exact: true })).toBeDefined();
    await waitFor(() => {
      expect(cards(view.container)).toEqual([['SCR-16:*', '/transparency']]);
    }, WAIT);
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it('desde el menú de otro rol: «Paquete mensual» y sin tarjetas de inicio', async () => {
    const { view } = await setup({ entry: true, twoZones: true, role: 'coordinator_sst' });
    await screen.findByRole('heading', { level: 1, name: text('navigation.scr12') }, WAIT);
    expect(view.container.querySelector('[data-slot]')).toBeNull();
  });
});

describe('inicio del COPASST (SCR-16)', { timeout: 40_000 }, () => {
  it('como su inicio: «Inicio», la nota de privacidad y sus avisos', async () => {
    const fixture = await transparencyFixture('/transparency', 'copasst');
    const { container } = render(<Copasst />, { wrapper: fixture.Wrapper });
    await screen.findByRole('heading', { level: 1, name: text('scr16.home.title') }, WAIT);
    expect(container.querySelector('[data-part="privacy"]')?.textContent).toBe(
      text('scr16.home.privacy'),
    );
    await screen.findByRole('link', { name: 'Zona sintética' }, WAIT);
    // Sin `notifications.read` en el fixture, «Notificaciones» no es una tarjeta: no la abriría.
    expect(cards(container)).toEqual([]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('desde el menú de otro rol: «Transparencia», sin nota de inicio', async () => {
    const fixture = await transparencyFixture('/transparency', 'coordinator_sst');
    const { container } = render(<Copasst />, { wrapper: fixture.Wrapper });
    await screen.findByRole('heading', { level: 1, name: text('navigation.scr16') }, WAIT);
    expect(container.querySelector('[data-part="privacy"]')).toBeNull();
  });
});

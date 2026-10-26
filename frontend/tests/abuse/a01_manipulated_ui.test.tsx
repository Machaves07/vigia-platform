/** La visibilidad manipulada no convierte a la interfaz en una barrera de autorización. */
import { screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { text } from '@/texts';
import { apiError, fixture, WAIT } from '../components/scr08/detail_support';

it('A-1 · BR-APP-01, 03, 43: aun forzando permisos locales, not_found no revela el hallazgo', async () => {
  // Se fuerza findings.read en el estado local y se monta el detalle sin pasar por la guarda.
  // MSW representa la decisión del servidor, NO prueba su autorización interna.
  const env = await fixture({ permissions: ['findings.read', 'evidence.read'] });
  env.responses.detail = () => apiError('not_found', 404);
  env.responses.history = () => apiError('not_found', 404);
  const view = env.view();
  expect(
    await screen.findAllByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT),
  ).toHaveLength(2);
  expect(env.calls.detail).toBe(1);
  expect(env.calls.record).toBe(0);
  expect(view.container.querySelector('[data-part="summary"]')).toBeNull();
  expect(screen.queryByRole('region', { name: text('scr08.detail.summary') })).toBeNull();
  expect(screen.queryByRole('button', { name: /clasificar|cerrar/i })).toBeNull();
}, 30_000);

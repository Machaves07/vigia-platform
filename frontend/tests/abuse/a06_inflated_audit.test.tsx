import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { expect, it } from 'vitest';
import { text } from '@/texts';
import { fixture as detailFixture } from '../components/scr08/detail_support';
import { API, EXPORT, fixture as exportsFixture, job, WAIT } from '../components/scr14/support';
import { server } from '../msw/server';
import { settle } from '../support/queryCache';

it('A-6 · BR-APP-54, 57: ninguna lectura auditada por precarga o refresco de 60 s, ningún envío por tecla', async () => {
  const env = await detailFixture();
  expect(env.calls.detail).toBe(0);
  env.view(); // Abrir por navegación es una intención explícita, a diferencia de precargar código.
  await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
  await waitFor(() => {
    expect(env.cache.client.isFetching()).toBe(0);
  }, WAIT);
  for (let minute = 0; minute < 5; minute++) {
    await act(async () => {
      env.clock.advance(60_000);
      await settle();
    });
  }
  expect(env.calls.detail).toBe(1);
  expect(env.calls.history).toBe(1);
  expect(env.calls.record).toBe(1);
  expect(env.calls.coverage).toHaveLength(1);
  expect(env.calls.readUrl).toBe(0);
  cleanup();
  await exportsFixture();
  const queries: URL[] = [];
  server.use(
    http.get(`${API}/accreditation/exports/:export_id`, ({ request }) => {
      queries.push(new URL(request.url));
      return HttpResponse.json(job());
    }),
  );
  await userEvent.setup().type(screen.getByLabelText(text('scr14.export_id')), EXPORT);
  await act(settle);
  expect(queries).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: text('scr14.open') }));
  await waitFor(() => {
    expect(queries).toHaveLength(1);
  }, WAIT);
  expect(queries[0]?.pathname).toBe(`/accreditation/exports/${EXPORT}`);
}, 30_000);

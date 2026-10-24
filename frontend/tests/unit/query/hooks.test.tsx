/**
 * `useScreenQuery` y `useScreenMutation` sobre la interfaz mínima (`read({ signal })`,
 * `write(input)`): una lectura auditada no sale al montar ni al volver a montar, solo con
 * `refresh()`; una escritura no se reintenta e invalida por recurso, también `GET /me` cuando
 * afecta al propio usuario (BR-APP-54 y 55).
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  policyFor,
  screenKey,
  ScreenQueryProvider,
  useScreenMutation,
  useScreenQuery,
  type ScreenQueryCache,
  type ScreenRead,
} from '@/state/query';

import { queryFixture } from '../../support/queryCache';

const WAIT = { timeout: 10_000 };
const caches: ScreenQueryCache[] = [];
afterEach(() => {
  for (const cache of caches.splice(0)) cache.dispose();
});

function setup() {
  const { cache } = queryFixture();
  caches.push(cache);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ScreenQueryProvider cache={cache}>{children}</ScreenQueryProvider>
  );
  return { cache, wrapper };
}

function counted<T>(value: T): {
  read: ScreenRead<T>;
  calls: () => number;
  signals: AbortSignal[];
} {
  const signals: AbortSignal[] = [];
  return {
    read: ({ signal }) => {
      signals.push(signal);
      return Promise.resolve(value);
    },
    calls: () => signals.length,
    signals,
  };
}

const detailKey = screenKey({
  screen: 'scr08',
  operation: 'GET /loop/findings/{finding_state_id}',
  scope: 'all',
  concession: null,
  params: { finding_state_id: 'f-1' },
});
const inboxKey = screenKey({
  screen: 'scr08',
  operation: 'GET /loop/findings',
  scope: 'all',
  concession: null,
});
const meKey = screenKey({ screen: 'shell', operation: 'GET /me', scope: 'all', concession: null });
const exportKey = screenKey({
  screen: 'scr14',
  operation: 'GET /accreditation/exports/{export_id}',
  scope: 'all',
  concession: null,
  params: { export_id: 'e-1' },
});

describe('useScreenQuery', () => {
  it('una lectura auditada no sale al montar; refresh() la pide una vez con señal de aborto', async () => {
    const { wrapper } = setup();
    const detail = counted({ id: 'f-1' });
    const policy = policyFor('GET /loop/findings/{finding_state_id}');
    const { result, unmount } = renderHook(
      () => useScreenQuery({ key: detailKey, read: detail.read, policy }),
      { wrapper },
    );
    expect(detail.calls()).toBe(0);
    expect(result.current.fetchedAt).toBeNull();
    await act(() => result.current.refresh());
    expect(detail.calls()).toBe(1);
    expect(detail.signals[0]).toBeInstanceOf(AbortSignal);
    await waitFor(() => {
      expect(result.current.data).toEqual({ id: 'f-1' });
    }, WAIT);
    expect(result.current.fetchedAt).not.toBeNull();
    unmount();

    // Volver a montar (también activada) no repite la lectura auditada.
    renderHook(() => useScreenQuery({ key: detailKey, read: detail.read, policy, enabled: true }), {
      wrapper,
    });
    expect(detail.calls()).toBe(1);
  });

  it('una lectura no auditada sale al montar', async () => {
    const { wrapper } = setup();
    const inbox = counted([]);
    const { result } = renderHook(
      () =>
        useScreenQuery({
          key: inboxKey,
          read: inbox.read,
          policy: policyFor('GET /loop/findings'),
        }),
      { wrapper },
    );
    await waitFor(() => {
      expect(result.current.status).toBe('success');
    }, WAIT);
    expect(inbox.calls()).toBe(1);
  });

  it('sin ScreenQueryProvider falla con un mensaje claro', () => {
    expect(() =>
      renderHook(() =>
        useScreenQuery({
          key: inboxKey,
          read: counted([]).read,
          policy: policyFor('GET /loop/findings'),
        }),
      ),
    ).toThrow('Falta ScreenQueryProvider');
  });
});

describe('useScreenMutation', () => {
  it('no reintenta una escritura que falla e invalida igualmente su pantalla', async () => {
    const { cache, wrapper } = setup();
    cache.client.setQueryData(inboxKey, []);
    cache.client.setQueryData(exportKey, {});
    let calls = 0;
    const { result } = renderHook(
      () =>
        useScreenMutation({
          write: () => {
            calls++;
            return Promise.reject(new Error('temporarily_unavailable'));
          },
          invalidates: { screen: 'scr08', resources: [] },
        }),
      { wrapper },
    );
    await act(async () => {
      await expect(result.current.run({})).rejects.toThrow('temporarily_unavailable');
    });
    expect(calls).toBe(1);
    expect(cache.client.getQueryState(inboxKey)?.isInvalidated).toBe(true);
    expect(cache.client.getQueryState(exportKey)?.isInvalidated).toBe(false);
  });

  it('invalida GET /me solo si la escritura afecta al propio usuario', async () => {
    const { cache, wrapper } = setup();
    cache.client.setQueryData(meKey, { user_id: 'u-1' });
    const { result } = renderHook(
      () =>
        useScreenMutation({
          write: (input: { user_id: string }) => Promise.resolve(input),
          invalidates: (input: { user_id: string }) => ({
            screen: 'scr02',
            resources: ['concessions'],
            affectsSelf: input.user_id === 'u-1',
          }),
        }),
      { wrapper },
    );
    await act(() => result.current.run({ user_id: 'u-2' }));
    expect(cache.client.getQueryState(meKey)?.isInvalidated).toBe(false);
    await act(() => result.current.run({ user_id: 'u-1' }));
    expect(cache.client.getQueryState(meKey)?.isInvalidated).toBe(true);
  });

  it('tras escribir, la lectura auditada montada queda desactualizada sin volver a pedirse', async () => {
    const { wrapper } = setup();
    const detail = counted({ id: 'f-1' });
    const policy = policyFor('GET /loop/findings/{finding_state_id}');
    const { result } = renderHook(
      () => ({
        query: useScreenQuery({ key: detailKey, read: detail.read, policy, enabled: true }),
        mutation: useScreenMutation({
          write: () => Promise.resolve(null),
          invalidates: { screen: 'scr09', resources: ['findings'] },
        }),
      }),
      { wrapper },
    );
    await waitFor(() => {
      expect(result.current.query.status).toBe('success');
    }, WAIT);
    await act(() => result.current.mutation.run(undefined));
    await waitFor(() => {
      expect(result.current.query.isStale).toBe(true);
    }, WAIT);
    expect(detail.calls()).toBe(1);
  });
});

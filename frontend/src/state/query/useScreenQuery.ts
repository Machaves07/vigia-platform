/**
 * Ganchos de pantalla sobre la caché (LC-APP-11): `useScreenQuery({ key, read, policy })` y
 * `useScreenMutation({ write, invalidates })`.
 *
 * Solo conocen una interfaz mínima (`read({ signal })` y `write(input)`), nunca `ports.*`: las
 * operaciones de `defineScreenPort` (VIG-292) la cumplen y la integración se prueba en el armazón
 * (VIG-320) y en cada pantalla.
 *
 * Una lectura `on_demand` (auditada) arranca desactivada: solo la pide `refresh()` o
 * `enabled: true`, y cualquiera de los dos debe salir de una acción explícita de la persona en
 * esta pantalla (botón, Intro, abrir un elemento). Nunca se vuelve a pedir al montar, al
 * invalidarse ni por el refresco periódico (BR-APP-54, A-6).
 */
import {
  QueryClientProvider,
  useMutation,
  useQuery,
  type QueryObserverOptions,
} from '@tanstack/react-query';
import { createContext, createElement, useContext, type ReactNode } from 'react';

import type { ScreenQueryCache } from '@/state/query/client';
import type { Invalidation } from './invalidation';
import type { ScreenQueryKey } from './keys';
import type { QueryPolicy } from './policies';

/** Lectura de pantalla: cancela con `signal` cuando la caché la aborta. */
export type ScreenRead<T> = (context: { readonly signal: AbortSignal }) => Promise<T>;

/** Escritura de pantalla. */
type ScreenWrite<Input, Result> = (input: Input) => Promise<Result>;

export interface ScreenQueryOptions<T> {
  readonly key: ScreenQueryKey;
  readonly read: ScreenRead<T>;
  readonly policy: QueryPolicy;
  /**
   * Por omisión, activada salvo las lecturas `on_demand`; en esas solo vale `true` tras una acción
   * explícita de la persona en esta pantalla.
   */
  readonly enabled?: boolean;
}

/** Opciones de TanStack Query de una lectura de pantalla (las comparten el gancho y las pruebas). */
export function screenQueryOptions<T>(
  options: ScreenQueryOptions<T>,
): QueryObserverOptions<T, Error, T, T, ScreenQueryKey> {
  const { explicitOnly, staleTimeMs } = options.policy;
  return {
    queryKey: options.key,
    queryFn: ({ signal }) => options.read({ signal }),
    staleTime: staleTimeMs,
    enabled: options.enabled ?? !explicitOnly,
    // Una auditada ya pedida no se repite al volver a montar la pantalla.
    refetchOnMount: !explicitOnly,
    meta: { policy: options.policy },
  };
}

const CacheContext = createContext<ScreenQueryCache | null>(null);

/** Da la caché a los ganchos (y su `QueryClient` a TanStack Query). */
export function ScreenQueryProvider(props: {
  readonly cache: ScreenQueryCache;
  readonly children?: ReactNode;
}) {
  return createElement(
    QueryClientProvider,
    { client: props.cache.client },
    createElement(CacheContext.Provider, { value: props.cache }, props.children),
  );
}

function useCache(): ScreenQueryCache {
  const cache = useContext(CacheContext);
  if (cache === null) throw new Error('Falta ScreenQueryProvider por encima de la pantalla.');
  return cache;
}

export interface ScreenQueryResult<T> {
  readonly data: T | undefined;
  readonly error: Error | null;
  readonly status: 'pending' | 'error' | 'success';
  readonly isFetching: boolean;
  readonly isStale: boolean;
  /** Instante de la última respuesta (para «actualizado hace X»), o `null`. */
  readonly fetchedAt: number | null;
  /** Pide la lectura otra vez: solo desde una acción explícita de la persona. */
  readonly refresh: () => Promise<void>;
}

export function useScreenQuery<T>(options: ScreenQueryOptions<T>): ScreenQueryResult<T> {
  const cache = useCache();
  const result = useQuery(screenQueryOptions(options), cache.client);
  return {
    data: result.data,
    error: result.error,
    status: result.status,
    isFetching: result.isFetching,
    isStale: result.isStale,
    fetchedAt: result.dataUpdatedAt === 0 ? null : result.dataUpdatedAt,
    refresh: async () => {
      await result.refetch({ cancelRefetch: false });
    },
  };
}

export interface ScreenMutationOptions<Input, Result> {
  readonly write: ScreenWrite<Input, Result>;
  /** Fija, o calculada por escritura (p. ej. `affectsSelf` si el usuario es el propio). */
  readonly invalidates: Invalidation | ((input: Input, result: Result | undefined) => Invalidation);
}

export interface ScreenMutationResult<Input, Result> {
  readonly run: (input: Input) => Promise<Result>;
  readonly status: 'idle' | 'pending' | 'error' | 'success';
  readonly error: Error | null;
  readonly reset: () => void;
}

/** Escritura de pantalla: sin reintentos e invalidando por recurso al terminar (BR-APP-55). */
export function useScreenMutation<Input, Result>(
  options: ScreenMutationOptions<Input, Result>,
): ScreenMutationResult<Input, Result> {
  const cache = useCache();
  const mutation = useMutation<Result, Error, Input>(
    {
      mutationFn: options.write,
      // También tras un fallo: el servidor pudo aceptar la escritura antes de que se perdiera
      // la respuesta.
      onSettled: (result, _error, input) => {
        const { invalidates } = options;
        return cache.invalidate(
          typeof invalidates === 'function' ? invalidates(input, result) : invalidates,
        );
      },
    },
    cache.client,
  );
  return {
    run: mutation.mutateAsync,
    status: mutation.status,
    error: mutation.error,
    reset: mutation.reset,
  };
}

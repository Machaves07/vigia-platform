/** Integración de SCR-16 con los proveedores de sesión y consulta del armazón (VIG-320). */
import type { ReactNode } from 'react';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import {
  transparencyClock,
  transparencyFailure,
  transparencyPort,
} from '@/ports/scr16Transparency';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { ScreenState } from '@/pages/shared/ScreenState';

export function useTransparencyContext() {
  const { principal } = useUiSession();
  const scope = principal?.organization.organization_id ?? 'all';
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr16', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => transparencyPort.hierarchy.read({}, signal),
  });
  return { scope, concession, hierarchy, plants: hierarchy.data?.plants ?? [] };
}

/** «Actualizado hace X» con el refresco que declara la política de la operación. */
export function TransparencyRefresh({
  query,
  operation,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
  readonly operation: ReadOperation;
}) {
  return (
    <RefreshIndicator
      clock={transparencyClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy={policyFor(operation).periodic ? 'periodic_60s' : 'on_demand'}
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function TransparencyResult({
  query,
  empty,
  children,
}: {
  readonly query: { status: string; error: Error | null };
  readonly empty: string | null;
  readonly children: ReactNode;
}) {
  if (query.status === 'pending') return <ScreenState state="loading" />;
  if (query.error !== null) {
    const failure = transparencyFailure(query.error);
    if (failure.state === 'error')
      return (
        <ScreenState
          state="error"
          {...(failure.correlationId === undefined ? {} : { correlationId: failure.correlationId })}
        />
      );
    return <ScreenState state={failure.state} />;
  }
  if (empty !== null) return <ScreenState state="empty" message={empty} />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

/** Integración con los proveedores de sesión y consulta del armazón (VIG-320). */
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { fleetClock, fleetFailure, fleetPort } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';

export function useFleetContext() {
  const { principal } = useUiSession();
  const scope = principal?.organization.organization_id ?? 'all';
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr07', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => fleetPort.hierarchy.read({}, signal),
  });
  return { scope, concession, plants: hierarchy.data?.plants ?? [] };
}

export function FleetNav() {
  return (
    <nav aria-label={text('scr07.title')}>
      <Link to="/fleet">{text('scr07.title')}</Link>
      {' · '}
      <Link to="/fleet/alarms">{text('scr07.alarms')}</Link>
    </nav>
  );
}

export function FleetRefresh({
  query,
  operation,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
  readonly operation: ReadOperation;
}) {
  return (
    <RefreshIndicator
      clock={fleetClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy={policyFor(operation).periodic ? 'periodic_60s' : 'on_demand'}
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function FleetResult({
  query,
  empty,
  children,
}: {
  readonly query: { status: string; error: Error | null };
  readonly empty: boolean;
  readonly children: ReactNode;
}) {
  if (query.status === 'pending') return <ScreenState state="loading" />;
  if (query.error !== null) {
    const failure = fleetFailure(query.error);
    if (failure.state === 'error')
      return (
        <ScreenState
          state="error"
          {...(failure.correlationId === undefined ? {} : { correlationId: failure.correlationId })}
        />
      );
    return <ScreenState state={failure.state} />;
  }
  if (empty) return <ScreenState state="empty" message={text('scr07.empty')} />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

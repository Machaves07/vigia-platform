/** Proveedores y estados comunes de las dos rutas de SCR-03. */
import type { ReactNode } from 'react';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { integrityClock, integrityFailure, integrityPort } from '@/ports/scr03Integrity';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

export function useIntegrityContext() {
  const { principal } = useUiSession();
  const organizationId = principal?.organization.organization_id ?? '';
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr03',
      operation: 'GET /hierarchy',
      scope: organizationId,
      concession,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => integrityPort.hierarchy.read({}, signal),
  });
  return {
    organizationId,
    // Bajo concesión, la jerarquía es la de la organización cliente.
    concessionOrganizationId:
      concession === null ? null : (hierarchy.data?.organization.organization_id ?? null),
    concession,
    hierarchy,
    plants: hierarchy.data?.plants ?? [],
  };
}

export function IntegrityRefresh({
  query,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
}) {
  return (
    <RefreshIndicator
      clock={integrityClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy="on_demand"
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function IntegrityQueryState({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null; isFetching: boolean };
  readonly children: ReactNode;
}) {
  if (query.isFetching) return <ScreenState state="loading" />;
  if (query.error !== null) return <ScreenState state={integrityFailure(query.error)} />;
  if (query.status === 'pending') return <p>{text('scr03.not_requested')}</p>;
  return <ScreenState state="ready">{children}</ScreenState>;
}

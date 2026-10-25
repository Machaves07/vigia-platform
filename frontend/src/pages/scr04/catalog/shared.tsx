/** Consulta acotada por organización, concesión y recurso de la ruta. */
import type { ReactNode } from 'react';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { catalogClock, catalogFailure } from '@/ports/scr04Catalog';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

export function useCatalogQuery<T>(
  operation: ReadOperation,
  resource: string,
  read: (signal: AbortSignal) => Promise<T>,
  enabled = true,
) {
  const { principal } = useUiSession();
  return useScreenQuery({
    key: screenKey({
      screen: 'scr04',
      operation,
      scope: `${principal?.organization.organization_id ?? 'all'}:${resource}`,
      concession: principal?.concession_id ?? null,
    }),
    policy: policyFor(operation),
    read: ({ signal }) => read(signal),
    enabled,
  });
}
export function Result({
  query,
  children,
  empty = false,
}: {
  readonly query: { status: string; error: Error | null };
  readonly children: ReactNode;
  readonly empty?: boolean;
}) {
  if (query.status === 'pending') return <ScreenState state="loading" />;
  if (query.error !== null) return <ScreenState state={catalogFailure(query.error)} />;
  if (empty) return <ScreenState state="empty" message={text('scr04.catalog.empty')} />;
  return <ScreenState state="ready">{children}</ScreenState>;
}
export function Refresh({
  query,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
}) {
  return (
    <RefreshIndicator
      clock={catalogClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy="on_demand"
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

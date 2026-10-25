/** Integración local de las dos secciones de SCR-01 con sesión, caché y estados comunes. */
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { hierarchyClock, hierarchyFailure } from '@/ports/scr01Hierarchy';
import { policyFor, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

export function useHierarchyScope() {
  const { principal } = useUiSession();
  return {
    scope: principal?.organization.organization_id ?? 'all',
    concession: principal?.concession_id ?? null,
  };
}

export function HierarchyNav() {
  return (
    <nav aria-label={text('scr01_hierarchy.navigation')}>
      <Link to="/admin/hierarchy">{text('scr01_hierarchy.title')}</Link>
      {' · '}
      <Link to="/admin/organization">{text('scr01_hierarchy.organization')}</Link>
    </nav>
  );
}

export function HierarchyRefresh({
  query,
  operation,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
  readonly operation: ReadOperation;
}) {
  return (
    <RefreshIndicator
      clock={hierarchyClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy={policyFor(operation).periodic ? 'periodic_60s' : 'on_demand'}
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function HierarchyResult({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null };
  readonly children: ReactNode;
}) {
  if (query.status === 'pending') return <ScreenState state="loading" />;
  if (query.error !== null) return <ScreenState state={hierarchyFailure(query.error)} />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

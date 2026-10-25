/** Integración local de SCR-04 con sesión, caché, avisos y estados. */
import { useId, type ReactNode } from 'react';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { parametersClock, parametersFailure, parametersNotice } from '@/ports/scr04Parameters';
import { useUiSession } from '@/state/session';

export function useParametersScope() {
  const { principal } = useUiSession();
  return {
    scope: principal?.organization.organization_id ?? 'all',
    concession: principal?.concession_id ?? null,
  };
}

export function Result({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null };
  readonly children: ReactNode;
}) {
  if (query.status === 'pending') return <ScreenState state="loading" />;
  if (query.error !== null) return <ScreenState state={parametersFailure(query.error)} />;
  return <>{children}</>;
}

export function Failure({ error }: { readonly error: Error | null }) {
  const id = useId();
  const notice = parametersNotice(error);
  return notice === null ? null : <FieldNotice id={id} notice={notice} />;
}

export function Refresh({
  query,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
}) {
  return (
    <RefreshIndicator
      clock={parametersClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy="on_demand"
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

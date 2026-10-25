/** Estados y frescura comunes; ningún temporizador consulta el servidor. */
import type { ReactNode } from 'react';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { exportFailure, exportsClock } from '@/ports/scr14Exports';
import { text } from '@/texts';

export function Refresh({
  query,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
}) {
  return (
    <RefreshIndicator
      clock={exportsClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy="on_demand"
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function Result({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null; isFetching: boolean };
  readonly children: ReactNode;
}) {
  if (query.isFetching) return <ScreenState state="loading" />;
  if (query.error !== null) return <ScreenState state={exportFailure(query.error)} />;
  if (query.status === 'pending') return <p>{text('scr14.not_requested')}</p>;
  return <ScreenState state="ready">{children}</ScreenState>;
}

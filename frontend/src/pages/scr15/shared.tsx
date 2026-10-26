/** Consultas propias por persona, organización, alcance y concesión; refresco compartido D-4. */
import { useSyncExternalStore, type ReactNode } from 'react';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { notificationsClock, notificationsFailure } from '@/ports/scr15Notifications';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import type { QueryParams } from '@/state/query/keys';
import { useUiSession } from '@/state/session';
import { useConcessionStore } from '@/state/concession';

const noSubscription = () => () => undefined;

export function useNotificationsContext() {
  const { principal } = useUiSession();
  const store = useConcessionStore();
  const active = useSyncExternalStore(
    store?.subscribe ?? noSubscription,
    () => store?.getSnapshot().active?.concession_id ?? null,
  );
  // La activación precede a GET /me: tampoco se consulta durante esa ventana.
  const concession = active ?? principal?.concession_id ?? null;
  return {
    scope: `${principal?.organization.organization_id ?? 'none'}:${principal?.user_id ?? 'none'}:${
      principal?.assignments
        .map((a) => `${a.scope_level}:${a.scope_id}`)
        .sort()
        .join(',') ?? ''
    }`,
    concession,
    // A-69: son datos propios; el servidor los rechaza bajo concesión.
    enabled:
      concession === null &&
      (principal?.effective_permissions.includes('notifications.read') ?? false),
  };
}

export function useNotificationsRead<T>(
  operation: ReadOperation,
  params: QueryParams,
  read: (signal: AbortSignal) => Promise<T>,
  shell = false,
) {
  const context = useNotificationsContext();
  return useScreenQuery({
    key: screenKey({
      screen: shell ? 'shell' : 'scr15',
      operation,
      scope: context.scope,
      concession: context.concession,
      params,
    }),
    policy: policyFor(operation),
    enabled: context.enabled,
    read: ({ signal }) => read(signal),
  });
}

export function NotificationsResult({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null; refresh(): Promise<void> };
  readonly children: ReactNode;
}) {
  if (query.error !== null) {
    const state = notificationsFailure(query.error);
    return state === 'error' ? (
      <ScreenState
        state="error"
        onRetry={() => {
          void query.refresh();
        }}
      />
    ) : (
      <ScreenState state={state} />
    );
  }
  if (query.status === 'pending') return <ScreenState state="loading" />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

export function NotificationsRefresh({
  query,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
}) {
  return (
    <RefreshIndicator
      clock={notificationsClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy="periodic_60s"
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

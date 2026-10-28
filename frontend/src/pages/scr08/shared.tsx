/** Contexto y estados de las dos vistas de proyección de SCR-08. */
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { homeScreen } from '@/app/navigation';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ScreenState } from '@/pages/shared/ScreenState';
import { findingsClock, findingsFailure, findingsPort, readOverdue } from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { descriptor } from './descriptor';

export function useFindingsContext() {
  const { principal } = useUiSession();
  const scope = `${principal?.organization.organization_id ?? 'none'}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr08', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => findingsPort.hierarchy.read({}, signal),
  });
  return {
    scope,
    concession,
    hierarchy,
    plants: hierarchy.data?.plants ?? [],
    permissions: principal?.effective_permissions ?? [],
  };
}

/**
 * Miga del panel y la bandeja (VIG-363): «Inicio del coordinador» solo si ese panel es el inicio de
 * la sesión (BR-APP-04) y la bandeja solo si la sesión la puede abrir; sin ninguna de las dos, nada.
 */
export function FindingsNav() {
  const { principal } = useUiSession();
  const permissions = new Set(principal?.effective_permissions ?? []);
  const home =
    principal === null ? null : homeScreen(principal.assignments, permissions, [descriptor]);
  const links = [
    ...(home?.group === 'home' ? [{ to: '/home', key: 'scr08.home.title' }] : []),
    ...(permissions.has('findings.read') ? [{ to: '/findings', key: 'scr08.inbox.title' }] : []),
  ];
  if (links.length === 0) return null;
  return (
    <nav aria-label={text('scr08.home.navigation')}>
      {links.map((link, index) => (
        <span key={link.to}>
          {index > 0 && ' · '}
          <Link to={link.to}>{text(link.key)}</Link>
        </span>
      ))}
    </nav>
  );
}

export function Result({
  query,
  empty = false,
  children,
}: {
  readonly query: { status: string; error: Error | null; refresh(): Promise<void> };
  readonly empty?: boolean;
  readonly children: ReactNode;
}) {
  if (query.error !== null) {
    const state = findingsFailure(query.error);
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
  if (empty) return <ScreenState state="empty" message={text('scr08.inbox.empty')} />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

export function Refresh({
  query,
  operation,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
  readonly operation: ReadOperation;
}) {
  return (
    <RefreshIndicator
      clock={findingsClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy={policyFor(operation).periodic ? 'periodic_60s' : 'on_demand'}
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function FindingTime({
  instant,
  timeZone,
}: {
  readonly instant: string;
  readonly timeZone: string;
}) {
  return <TimestampWithOffset instant={instant} timeZone={timeZone} />;
}

export function OverdueHeader({
  context,
}: {
  readonly context: ReturnType<typeof useFindingsContext>;
}) {
  const ids = context.plants.map((p) => p.plant_id);
  const allowed = context.permissions.includes('executive_summary.read');
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation: 'GET /loop/overdue',
      scope: context.scope,
      concession: context.concession,
      params: { plants: ids },
    }),
    policy: policyFor('GET /loop/overdue'),
    enabled: allowed && context.hierarchy.status === 'success',
    read: ({ signal }) => readOverdue(ids, signal),
  });
  if (!allowed) return null;
  return (
    <section aria-label={text('scr08.inbox.overdue')}>
      <h2>{text('scr08.inbox.overdue')}</h2>
      <>
        <Refresh query={query} operation="GET /loop/overdue" />
        <Result query={query}>
          {query.data?.some((p) => p.notice !== null) ? (
            <p role="status">{text('scr08.inbox.count_unavailable')}</p>
          ) : (
            <p data-part="overdue">
              {text('scr08.inbox.overdue')}:{' '}
              <strong>{query.data?.reduce((sum, p) => sum + p.count, 0)}</strong>
            </p>
          )}
        </Result>
      </>
    </section>
  );
}

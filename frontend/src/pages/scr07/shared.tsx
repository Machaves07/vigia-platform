/** Integración con los proveedores de sesión y consulta del armazón (VIG-320). */
import { fromZonedTime } from 'date-fns-tz';
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
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
  return {
    scope,
    concession,
    plants: hierarchy.data?.plants ?? [],
    nodes: hierarchy.data?.nodes ?? [],
  };
}

export function FleetNav() {
  return (
    <nav aria-label={text('scr07.title')}>
      <Link to="/fleet">{text('scr07.title')}</Link>
      {' · '}
      <Link to="/fleet/alarms">{text('scr07.alarms')}</Link>
      {' · '}
      <Link to="/fleet/enrollment">{text('scr07.declare.title')}</Link>
      {' · '}
      <Link to="/fleet/target-versions">{text('scr07.target_version.title')}</Link>
    </nav>
  );
}

/**
 * Aviso del servidor de una escritura: junto al campo que nombra (`field`) o, con `field="form"`,
 * los que no se anclan a ningún campo. El mensaje sale del mapa cerrado de errores (BR-APP-71).
 */
export function FleetNotice({
  notice,
  field,
  id,
}: {
  readonly notice: Notice | null;
  readonly field: string;
  readonly id: string;
}) {
  if (notice?.field !== field) return null;
  return <FieldNotice id={id} notice={notice} />;
}

/** `datetime-local` en la zona de la planta → instante UTC con milisegundos, o `null`. */
export function plantInstant(value: string, timeZone: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value) || timeZone === '') return null;
  const date = fromZonedTime(value, timeZone);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
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

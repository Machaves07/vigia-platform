/** Contexto y estados de las dos vistas de proyección de SCR-08. */
import { fromZonedTime } from 'date-fns-tz';
import type { ReactNode } from 'react';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ScreenState } from '@/pages/shared/ScreenState';
import { findingsFailure, findingsPort, readOverdue } from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

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
 * Zona horaria de los filtros por día (VIG-366, S3-11): la de la planta de la zona elegida; sin
 * zona, la de las plantas del alcance si todas comparten una. Solo si difieren, UTC (y la
 * etiqueta lo dice).
 */
export function filterTimeZone(
  plants: readonly {
    readonly timezone: string;
    readonly zones: readonly { readonly zone_id: string }[];
  }[],
  zoneId: string | null,
): string {
  const chosen = plants.find((plant) => plant.zones.some((zone) => zone.zone_id === zoneId));
  if (chosen !== undefined) return chosen.timezone;
  const zones = new Set(plants.map((plant) => plant.timezone));
  return zones.size === 1 ? (plants[0]?.timezone ?? 'UTC') : 'UTC';
}

/** Instante en que empieza el día `aaaa-mm-dd` en `timeZone`; `null` si no es válido. */
export function dayStart(day: string, timeZone: string): Date | null {
  const start = fromZonedTime(`${day}T00:00:00`, timeZone);
  return Number.isFinite(start.getTime()) ? start : null;
}

/** Instante en que termina el día `aaaa-mm-dd` en `timeZone` (inicio del siguiente). */
export function dayEnd(day: string, timeZone: string): Date | null {
  const next = new Date(Date.parse(`${day}T00:00:00.000Z`) + 86_400_000);
  if (!Number.isFinite(next.getTime())) return null;
  return dayStart(next.toISOString().slice(0, 10), timeZone);
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

export function FindingTime({
  instant,
  timeZone,
  zone = 'inline',
}: {
  readonly instant: string;
  readonly timeZone: string;
  /** `'header'` cuando la tabla nombra la zona en su cabecera (VIG-366). */
  readonly zone?: 'inline' | 'header';
}) {
  return <TimestampWithOffset instant={instant} timeZone={timeZone} zone={zone} />;
}

/** La zona común de las filas, si todas comparten una (para nombrarla en la cabecera). */
export function sharedTimeZone(timeZones: readonly string[]): string | null {
  const zones = new Set(timeZones);
  return zones.size === 1 ? (timeZones[0] ?? null) : null;
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
    </section>
  );
}

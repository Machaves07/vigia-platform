/** Lecturas y estados de SCR-12: claves por alcance, concesión y parámetros; solo a demanda. */
import type { ReactNode } from 'react';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import { monthlyClock, monthlyFailure, monthlyHttpError } from '@/ports/scr12Monthly';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

export function useMonthlyContext() {
  const { principal } = useUiSession();
  return {
    scope: `${principal?.organization.organization_id ?? 'none'}:${
      principal?.assignments
        .map((a) => `${a.scope_level}:${a.scope_id}`)
        .sort()
        .join(',') ?? ''
    }`,
    concession: principal?.concession_id ?? null,
    canCommit: principal?.effective_permissions.includes('commitments.write') ?? false,
    canReview: principal?.effective_permissions.includes('findings.classify') ?? false,
  };
}
export type MonthlyContext = ReturnType<typeof useMonthlyContext>;

export function useMonthlyRead<T>(
  context: MonthlyContext,
  operation: ReadOperation,
  params: Readonly<Record<string, string | number>>,
  read: (signal: AbortSignal) => Promise<T>,
  enabled = true,
) {
  return useScreenQuery({
    key: screenKey({
      screen: 'scr12',
      operation,
      scope: context.scope,
      concession: context.concession,
      params,
    }),
    policy: policyFor(operation),
    enabled,
    read: ({ signal }) => read(signal),
  });
}

export function MonthlyRefresh({
  query,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh: () => Promise<void> };
}) {
  return (
    <RefreshIndicator
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy="on_demand"
      clock={monthlyClock()}
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function MonthlyResult({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null };
  readonly children: ReactNode;
}) {
  if (query.error !== null) {
    const failure = monthlyFailure(query.error);
    return failure === 'unsealed' ? (
      <p role="status">{text('scr12.unsealed')}</p>
    ) : (
      <ScreenState state={failure} />
    );
  }
  if (query.status === 'pending') return <ScreenState state="loading" />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

/** Error del servidor anclado al formulario, sin descartar el borrador ni inferir reglas. */
export function MonthlyWriteError({
  error,
  id,
}: {
  readonly error: Error | null;
  readonly id: string;
}) {
  if (error === null) return null;
  if (monthlyFailure(error) === 'not_available_in_scope')
    return <ScreenState state="not_available_in_scope" />;
  return <FieldNotice id={id} notice={{ id, field: id, error: monthlyHttpError(error) }} />;
}

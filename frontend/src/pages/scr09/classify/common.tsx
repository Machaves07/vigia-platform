/** Contexto, estados y lecturas auditadas compartidos por los grupos `classify` y `loop`. */
import { useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router';
import { ScreenState } from '@/pages/shared/ScreenState';
import { classifyPort, loopFailure, type Plant } from '@/ports/scr09Classify';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

export function useLoopContext() {
  const { principal } = useUiSession();
  const { finding_state_id: findingId = '' } = useParams();
  const scope = `${principal?.organization.organization_id ?? 'none'}:${
    principal?.assignments
      .map((a) => `${a.scope_level}:${a.scope_id}`)
      .sort()
      .join(',') ?? ''
  }`;
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr09', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => classifyPort.hierarchy.read({}, signal),
  });
  const plants: readonly Plant[] = hierarchy.data?.plants ?? [];
  return {
    findingId,
    organizationId: principal?.organization.organization_id ?? '',
    scope,
    concession,
    plants,
    user: principal === null ? null : { id: principal.user_id, name: principal.display_name },
    permissions: principal?.effective_permissions ?? [],
  };
}

export type LoopContext = ReturnType<typeof useLoopContext>;

/** Zona horaria de la planta del hallazgo (UTC mientras la jerarquía no la da). */
export function plantTimeZone(context: LoopContext, plantId: string | undefined): string {
  return context.plants.find((plant) => plant.plant_id === plantId)?.timezone ?? 'UTC';
}

export function zoneName(context: LoopContext, zoneId: string | undefined): string | null {
  for (const plant of context.plants)
    for (const zone of plant.zones) if (zone.zone_id === zoneId) return zone.name;
  return null;
}

/** Número de apertura: cada montaje de la pantalla es una apertura con su propia lectura. */
let openings = 0;

/** La apertura de la pantalla: abrirla es la acción explícita que pide lo auditado (VIG-365). */
export function useOpening(): number {
  const [opening] = useState(() => (openings += 1));
  return opening;
}

/**
 * Lectura auditada (`on_demand`), como en el detalle (VIG-365, S3-22): abrir la pantalla es la
 * acción explícita que la pide, una vez por apertura (`opening` en la clave); después solo se
 * repite desde «Actualizar» o tras una escritura confirmada (`request()`), nunca sola.
 */
export function useAuditedRead<T>(
  context: LoopContext,
  operation: ReadOperation,
  opening: number,
  read: (signal: AbortSignal) => Promise<T>,
) {
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: { finding_state_id: context.findingId, opening },
    }),
    policy: policyFor(operation),
    // Abrir la pantalla es la acción explícita que pide lo auditado (como el detalle).
    enabled: true,
    read: ({ signal }) => read(signal),
  });
  return {
    ...query,
    request: () => {
      void query.refresh();
    },
  };
}

export function LoopNav({ findingId }: { readonly findingId: string }) {
  return (
    <nav aria-label={text('scr09.classify.navigation')}>
      <Link to={`/findings/${findingId}/classify`}>{text('scr09.classify.title')}</Link>
      {' · '}
      <Link to={`/findings/${findingId}/loop`}>{text('scr09.loop.title')}</Link>
    </nav>
  );
}

export function Result({
  query,
  children,
}: {
  readonly query: { status: string; error: Error | null; refresh(): Promise<void> };
  readonly children: ReactNode;
}) {
  if (query.error !== null) {
    const state = loopFailure(query.error);
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

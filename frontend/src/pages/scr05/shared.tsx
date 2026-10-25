/** Integración de SCR-05 con los proveedores de sesión y consulta del armazón (VIG-320). */
import { useId, type ReactNode } from 'react';
import { Link } from 'react-router';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import {
  gatesClock,
  gatesFailure,
  gatesNotice,
  gatesPort,
  localFailure,
  type PlantPolicy,
} from '@/ports/scr05Gates';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';
import styles from './scr05.module.css';

export function useGatesContext() {
  const { principal } = useUiSession();
  const scope = principal?.organization.organization_id ?? 'all';
  const concession = principal?.concession_id ?? null;
  const permissions = principal?.effective_permissions ?? [];
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr05', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => gatesPort.hierarchy.read({}, signal),
  });
  return {
    scope,
    concession,
    hierarchy,
    plants: hierarchy.data?.plants ?? [],
    /** Clave de permiso del `Principal`: el servidor decide; esto solo elige qué formularios pintar. */
    can: (permission: string) => permissions.includes(permission),
  };
}

export type GatesContext = ReturnType<typeof useGatesContext>;

/** Política de incerrables de la planta (aviso informativo de la zona y de la planta). */
export function usePlantPolicy(context: GatesContext, plantId: string | undefined) {
  return useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: 'GET /plants/{plant_id}/policy',
      scope: `${context.scope}:${plantId ?? ''}`,
      concession: context.concession,
      params: { plant_id: plantId ?? '' },
    }),
    policy: policyFor('GET /plants/{plant_id}/policy'),
    enabled: plantId !== undefined,
    read: ({ signal }) => gatesPort.plantPolicy.read({ path: { plant_id: plantId ?? '' } }, signal),
  });
}

/**
 * Aviso informativo «planta sin política de incerrables» (IF-11 paso 3, respuesta 4 = A de U-03):
 * se muestra desde el inicio y no deshabilita ni oculta ninguna acción; la política bloquea solo
 * la aprobación del uso, y eso lo responde el servidor (`catalog_plant_policy_missing`).
 */
export function PlantPolicyNotice({
  policy,
  plantId,
}: {
  readonly policy: PlantPolicy | undefined;
  readonly plantId: string;
}) {
  if (policy === undefined || policy.loaded) return null;
  return (
    <aside
      className={styles.notice}
      aria-label={text('scr05.policy_notice.title')}
      data-part="policy_notice"
    >
      <p>{text('scr05.policy_notice.body')}</p>
      <Link to={`/plants/${plantId}/policy`}>{text('scr05.policy_notice.link')}</Link>
    </aside>
  );
}

/** «Actualizado hace X» con el refresco que declara la política de la operación. */
export function GatesRefresh({
  query,
  operation,
}: {
  readonly query: { fetchedAt: number | null; isFetching: boolean; refresh(): Promise<void> };
  readonly operation: ReadOperation;
}) {
  return (
    <RefreshIndicator
      clock={gatesClock()}
      fetchedAt={query.fetchedAt}
      isFetching={query.isFetching}
      refreshPolicy={policyFor(operation).periodic ? 'periodic_60s' : 'on_demand'}
      onRefresh={() => {
        void query.refresh();
      }}
    />
  );
}

export function GatesResult({
  query,
  empty,
  children,
}: {
  readonly query: { status: string; error: Error | null };
  readonly empty: string | null;
  readonly children: ReactNode;
}) {
  if (query.status === 'pending') return <ScreenState state="loading" />;
  if (query.error !== null) {
    const failure = gatesFailure(query.error);
    if (failure.state === 'error')
      return (
        <ScreenState
          state="error"
          {...(failure.correlationId === undefined ? {} : { correlationId: failure.correlationId })}
        />
      );
    return <ScreenState state={failure.state} />;
  }
  if (empty !== null) return <ScreenState state="empty" message={empty} />;
  return <ScreenState state="ready">{children}</ScreenState>;
}

/**
 * El rechazo de una escritura: anclado al formulario con la etiqueta de su `detail_code` (errores
 * de §4 de las interfaces) o, sin respuesta del servidor, el estado común.
 */
export function WriteFailure({
  error,
  field,
}: {
  readonly error: Error | null;
  readonly field: string;
}) {
  const id = useId();
  const notice = gatesNotice(error, field);
  if (notice !== null) return <FieldNotice id={id} notice={notice} />;
  const local = localFailure(error);
  if (local === null) return null;
  return (
    <p role="alert">
      {text(local === 'unavailable' ? 'screen_state.unavailable' : 'screen_state.error')}
    </p>
  );
}

/** Plantas y zonas de la jerarquía por identificador. */
export function findZone(context: GatesContext, zoneId: string) {
  const plant = context.plants.find((entry) => entry.zones.some((zone) => zone.zone_id === zoneId));
  const zone = plant?.zones.find((entry) => entry.zone_id === zoneId);
  return { plant, zone };
}

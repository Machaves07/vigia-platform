/**
 * SCR-08, grupo `detail` (IF-08 pasos 1 y 2; SUP-U05-02 y 04; RV-12; BR-APP-36, 40, 54, 58 y 64).
 *
 * El detalle es una lectura auditada: abrirlo es la acción explícita que lo pide, una vez por
 * apertura (cada apertura tiene su propia clave de caché) y nunca por refresco. El historial de
 * clasificaciones se pide con él y se muestra completo, porque reclasificar no borra (P4). El
 * `Finding` de cada tramo sale del expediente y se valida con Ajv antes de pintarse.
 *
 * Recorrido guiado (VIG-359): con el detalle cargado, el acceso que admite su estado (cola,
 * clasificar, reclasificar o lazo; `FindingActions`), y la resolución de la cola en el historial.
 *
 * Degradación declarada: `503 loop_gate_state_unavailable` (fallo cerrado de U-04, el cuerpo no
 * trae el hallazgo) y `loop_standard_text_unavailable` degradan su sección con «reintentar», sin
 * abrir el circuito `platform`; el historial, el estado «ahora» y los demás datos siguen.
 */
import { useState } from 'react';
import { Link, useParams } from 'react-router';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { FindingActions } from '@/pages/shared/FindingActions';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  detailClock,
  detailFailure,
  detailPort,
  readFindingRecords,
  type FindingDetail,
  type Plant,
} from '@/ports/scr08Detail';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { DetailView, History, type NowState, type RecordsState } from './DetailView';

const DETAIL = 'GET /loop/findings/{finding_state_id}';
const HISTORY = 'GET /loop/findings/{finding_state_id}/classifications';
const RECORD = 'GET /ledger/records/{record_id}';
const COVERAGE = 'GET /zones/{zone_id}/coverage/at';

/** Número de apertura: cada montaje del detalle es una apertura con su propia lectura. */
let openings = 0;

function useDetailContext() {
  const { principal } = useUiSession();
  const { finding_state_id: findingId = '' } = useParams();
  const [opening] = useState(() => (openings += 1));
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
    read: ({ signal }) => detailPort.hierarchy.read({}, signal),
  });
  const plants: readonly Plant[] = hierarchy.data?.plants ?? [];
  return {
    findingId,
    opening,
    scope,
    concession,
    plants,
    permissions: new Set(principal?.effective_permissions ?? []),
  };
}

type DetailContext = ReturnType<typeof useDetailContext>;

function useAudited<T>(
  context: DetailContext,
  operation: ReadOperation,
  params: NonNullable<Parameters<typeof screenKey>[0]['params']>,
  enabled: boolean,
  read: (signal: AbortSignal) => Promise<T>,
) {
  return useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: { ...params, finding_state_id: context.findingId, opening: context.opening },
    }),
    policy: policyFor(operation),
    enabled,
    read: ({ signal }) => read(signal),
  });
}

function timeZoneOf(context: DetailContext, plantId: string | undefined): string {
  return context.plants.find((plant) => plant.plant_id === plantId)?.timezone ?? 'UTC';
}

function zoneOf(context: DetailContext, zoneId: string | undefined): string | null {
  for (const plant of context.plants)
    for (const zone of plant.zones) if (zone.zone_id === zoneId) return zone.name;
  return null;
}

function Failure({ error, onRetry }: { readonly error: Error; readonly onRetry: () => void }) {
  const failure = detailFailure(error);
  if (failure === 'gate_state_unavailable' || failure === 'standard_text_unavailable') {
    return (
      <div role="alert" data-part={failure}>
        <p>
          {text(
            failure === 'gate_state_unavailable'
              ? 'scr08.detail.gate_unavailable'
              : 'scr08.detail.standard_unavailable',
          )}
        </p>
        <button type="button" onClick={onRetry}>
          {text('scr08.detail.retry')}
        </button>
      </div>
    );
  }
  if (failure === 'error') return <ScreenState state="error" onRetry={onRetry} />;
  return <ScreenState state={failure} />;
}

function Finding({
  context,
  detail,
}: {
  readonly context: DetailContext;
  readonly detail: FindingDetail;
}) {
  const clock = detailClock();
  const [instant] = useState(() => new Date(clock.now()).toISOString());
  const withCoverage = context.permissions.has('coverage.read');
  const recordIds = detail.records.map((record) => record.record_id);
  const records = useAudited(context, RECORD, { records: recordIds }, true, (signal) =>
    readFindingRecords(detail.records, signal),
  );
  const coverage = useAudited(
    context,
    COVERAGE,
    { zone_id: detail.zone_id, instant },
    withCoverage,
    (signal) =>
      detailPort.coverageNow.read(
        { path: { zone_id: detail.zone_id }, query: { instant } },
        signal,
      ),
  );
  const now: NowState = !withCoverage
    ? { kind: 'hidden' }
    : coverage.data !== undefined
      ? { kind: 'ready', coverage: coverage.data }
      : coverage.error !== null
        ? { kind: 'unavailable' }
        : { kind: 'loading' };
  const recordState: RecordsState =
    records.data !== undefined
      ? { kind: 'ready', records: records.data }
      : records.error !== null
        ? { kind: 'unavailable' }
        : { kind: 'loading' };
  return (
    <DetailView
      detail={detail}
      zone={zoneOf(context, detail.zone_id)}
      timeZone={timeZoneOf(context, detail.plant_id)}
      now={now}
      records={recordState}
      clock={clock}
      canViewClips={context.permissions.has('evidence.read')}
    />
  );
}

export default function Page() {
  const context = useDetailContext();
  const path = { finding_state_id: context.findingId };
  const detail = useAudited(context, DETAIL, {}, true, (signal) =>
    detailPort.detail.read({ path }, signal),
  );
  const history = useAudited(context, HISTORY, {}, true, (signal) =>
    detailPort.history.read({ path }, signal),
  );
  const reload = () => {
    void detail.refresh();
    void history.refresh();
  };
  const timeZone = timeZoneOf(context, detail.data?.plant_id);
  return (
    <main>
      <nav aria-label={text('scr08.detail.navigation')}>
        <Link to="/findings">{text('scr08.detail.back')}</Link>
        {' · '}
        <Link to="/findings/labels">{text('scr08.detail.labels_link')}</Link>
      </nav>
      <h1>{text('scr08.detail.title')}</h1>
      <p>{text('scr08.detail.audited')}</p>
      {detail.data !== undefined && (
        <FindingActions
          findingId={context.findingId}
          state={detail.data.lifecycle_state}
          detectionId={detail.data.subject_kind === 'detection' ? detail.data.subject_id : null}
          permissions={context.permissions}
          landmark
        />
      )}
      <RefreshIndicator
        clock={detailClock()}
        fetchedAt={detail.fetchedAt}
        isFetching={detail.isFetching || history.isFetching}
        refreshPolicy="on_demand"
        onRefresh={reload}
      />
      <section aria-label={text('scr08.detail.title')} data-section="finding">
        {detail.error !== null ? (
          <Failure
            error={detail.error}
            onRetry={() => {
              void detail.refresh();
            }}
          />
        ) : detail.data === undefined ? (
          <ScreenState state="loading" />
        ) : (
          <Finding context={context} detail={detail.data} />
        )}
      </section>
      {history.error !== null ? (
        <section aria-label={text('scr08.detail.history')}>
          <h2>{text('scr08.detail.history')}</h2>
          <Failure
            error={history.error}
            onRetry={() => {
              void history.refresh();
            }}
          />
        </section>
      ) : history.data === undefined ? (
        <ScreenState state="loading" />
      ) : (
        <History
          classifications={history.data.classifications}
          reviewResolutions={history.data.review_resolutions}
          timeAggregated={history.data.time_aggregated}
          timeZone={timeZone}
        />
      )}
    </main>
  );
}

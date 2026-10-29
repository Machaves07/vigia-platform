/** IF-15: selección explícita, caché por alcance y secciones independientes ante fallos. */
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { formatInTimeZone, fromZonedTime } from 'date-fns-tz';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { HomeCards } from '@/pages/shared/HomeCards';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  acceptsOrganization,
  metricsClock,
  metricsFailure,
  metricsPort,
  type Plant,
  type MetricsContext,
  type Overdue,
} from '@/ports/scr13Metrics';
import { loadCoverage, periodQuery } from '@/state/coverage/compose';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import type { ReadOperation } from '@/state/query/policies';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { AdoptionView, ExposureView, MetricView, OverdueView } from './Views';
import styles from './scr13.module.css';

const WEEK_MS = 7 * 86_400_000;

/** El mes de negocio usa la zona horaria de la planta, no la del navegador. */
function monthRange(month: string, timeZone: string) {
  const [year, part] = month.split('-').map(Number);
  if (year === undefined || part === undefined) throw new Error('Periodo inválido.');
  const next =
    part === 12 ? `${String(year + 1)}-01` : `${String(year)}-${String(part + 1).padStart(2, '0')}`;
  return {
    fromMs: fromZonedTime(`${month}-01T00:00:00`, timeZone).getTime(),
    toMs: fromZonedTime(`${next}-01T00:00:00`, timeZone).getTime(),
  };
}

interface QueryState {
  readonly status: string;
  readonly error: Error | null;
  readonly isFetching: boolean;
  readonly fetchedAt: number | null;
  readonly refresh: () => Promise<void>;
}
function Section({
  query,
  children,
}: {
  readonly query: QueryState;
  readonly children: ReactNode;
}) {
  let body = children;
  if (query.isFetching || query.status === 'pending') body = <ScreenState state="loading" />;
  else if (query.error !== null) {
    const failure = metricsFailure(query.error);
    body =
      failure === 'not_computable' ? (
        <p>{text('scr13.not_computable')}</p>
      ) : (
        <ScreenState state={failure} />
      );
  }
  return (
    <div className={styles.section}>
      <RefreshIndicator
        clock={metricsClock()}
        fetchedAt={query.fetchedAt}
        isFetching={query.isFetching}
        refreshPolicy="on_demand"
        onRefresh={() => {
          void query.refresh();
        }}
      />
      {body}
    </div>
  );
}

/**
 * Los 4 indicadores del resumen del gerente (sistema visual v1, «Gerente · Inicio»; VIG-365): las
 * cifras del servidor tal como llegan, en este orden y sin recalcular; «No calculable» si el
 * servidor no la calcula (RF-PLA-19, nunca cero). El detalle de cada una sigue debajo.
 */
const WEEKLY_INDICATORS = [
  'exposure',
  'classified_in_time',
  'primary_engineering_closures',
  'overdue',
] as const;

function WeeklyIndicators({
  values,
}: {
  readonly values: readonly { key: string; status: string; value?: number | null }[];
}) {
  return (
    <section aria-label={text('scr13.home.indicators')}>
      <h2>{text('scr13.home.indicators')}</h2>
      <ul className={styles.indicators}>
        {WEEKLY_INDICATORS.map((key) => {
          const metric = values.find((value) => value.key === key);
          const computable =
            metric !== undefined && metric.status !== 'not_computable' && metric.value != null;
          return (
            <li key={key} className={styles.indicator} data-indicator={key}>
              <h3>{text(`scr13.${key}`)}</h3>
              <strong>{computable ? metric.value : text('scr13.not_computable')}</strong>
              {metric?.status === 'partial' && <p>{text('scr13.partial')}</p>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Results({
  plant,
  zoneId,
  month,
  context,
  weekly,
  permissions,
  concession,
}: {
  readonly plant: Plant;
  readonly zoneId: string;
  readonly month: string;
  readonly context: MetricsContext;
  readonly weekly: boolean;
  readonly permissions: readonly string[];
  readonly concession: string | null;
}) {
  const period = monthRange(month, plant.timezone);
  const zoneName = plant.zones.find((zone) => zone.zone_id === zoneId)?.name ?? text('scr13.zone');
  const params = { plant_id: plant.plant_id, zone_id: zoneId, period: month };
  const key = (operation: ReadOperation, extra: Readonly<Record<string, string>> = {}) =>
    screenKey({
      screen: 'scr13',
      operation,
      scope: context.organizationId,
      concession,
      params: { ...params, ...extra },
    });
  const canMetrics = permissions.includes('metrics.read');
  const canExposure = permissions.includes('exposure.read');
  const metrics = useScreenQuery({
    key: key('GET /accreditation/metrics'),
    policy: policyFor('GET /accreditation/metrics'),
    enabled: canMetrics,
    read: async ({ signal }) => {
      const data = await metricsPort.metrics.read(
        { query: { zone_id: zoneId, period: month } },
        signal,
      );
      if (
        data.plant_id !== plant.plant_id ||
        data.period !== month ||
        data.zones.some(
          (zone) =>
            zone.zone_id !== zoneId ||
            zone.plant_id !== plant.plant_id ||
            zone.period !== month ||
            !acceptsOrganization(zone.organization_id, context),
        )
      )
        throw new Error('Métricas fuera de alcance.');
      return data;
    },
  });
  const exposure = useScreenQuery({
    key: key('GET /accreditation/exposure'),
    policy: policyFor('GET /accreditation/exposure'),
    enabled: canExposure,
    read: async ({ signal }) => {
      const data = await metricsPort.exposure.read(
        { query: { zone_id: zoneId, period: month } },
        signal,
      );
      if (
        data.zone_id !== zoneId ||
        data.period !== month ||
        Date.parse(data.period_from) !== period.fromMs ||
        Date.parse(data.period_to) !== period.toMs
      )
        throw new Error('Exposición fuera de alcance.');
      return data;
    },
  });
  const coverage = useScreenQuery({
    key: key('GET /zones/{zone_id}/coverage'),
    policy: policyFor('GET /zones/{zone_id}/coverage'),
    enabled: canExposure,
    read: ({ signal }) =>
      loadCoverage({
        period,
        signal,
        clock: metricsClock(),
        fetchChunk: async (chunk, chunkSignal) => {
          const data = await metricsPort.coverage.read(
            { path: { zone_id: zoneId }, query: periodQuery(chunk) },
            chunkSignal,
          );
          if (
            !acceptsOrganization(data.organization_id, context) ||
            data.zone_id !== zoneId ||
            data.plant_id !== plant.plant_id
          )
            throw new Error('Cobertura fuera de alcance.');
          return data;
        },
      }),
  });
  const adoption = useScreenQuery({
    key: key('GET /accreditation/adoption'),
    policy: policyFor('GET /accreditation/adoption'),
    enabled: canMetrics,
    read: async ({ signal }) => {
      const data = await metricsPort.adoption.read(
        {
          query: {
            zone_id: zoneId,
            from: month + '-01',
            to: formatInTimeZone(period.toMs - 1, plant.timezone, 'yyyy-MM-dd'),
          },
        },
        signal,
      );
      if (data.plant_id !== plant.plant_id || data.zones.some((zone) => zone.zone_id !== zoneId))
        throw new Error('Adopción fuera de alcance.');
      return data;
    },
  });
  const [cursor, setCursor] = useState<string | null>(null);
  const [previousItems, setPreviousItems] = useState<Overdue['items']>([]);
  const overdue = useScreenQuery({
    key: key('GET /loop/overdue', { cursor: cursor ?? '' }),
    policy: policyFor('GET /loop/overdue'),
    enabled: weekly && permissions.includes('executive_summary.read'),
    read: async ({ signal }) => {
      const data = await metricsPort.overdue.read(
        {
          query: {
            plant_id: plant.plant_id,
            zone_id: [zoneId],
            cursor,
            limit: 200,
          },
        },
        signal,
      );
      if (
        data.plant_id !== plant.plant_id ||
        data.zones.some((zone) => zone.zone_id !== zoneId) ||
        data.items.some((item) => item.plant_id !== plant.plant_id || item.zone_id !== zoneId)
      )
        throw new Error('Vencidos fuera de alcance.');
      return data;
    },
  });
  return (
    <>
      {weekly && canMetrics && metrics.data !== undefined && (
        <WeeklyIndicators values={metrics.data.zones[0]?.values ?? []} />
      )}
      {canMetrics && (
        <Section query={metrics}>
          {metrics.data?.zones.length === 0 ? (
            <ScreenState state="empty" message={text('scr13.empty')} />
          ) : (
            metrics.data?.zones.map((zone) => (
              <MetricView key={zone.snapshot_id} data={zone} zoneName={zoneName} {...context} />
            ))
          )}
        </Section>
      )}
      {canExposure && (
        <div data-section="exposure">
          <Section query={exposure}>
            <Section query={coverage}>
              {exposure.data !== undefined && coverage.data !== undefined && (
                <ExposureView
                  data={exposure.data}
                  composite={coverage.data}
                  clock={metricsClock()}
                  timeZone={plant.timezone}
                  onPeriodChange={() => undefined}
                />
              )}
            </Section>
          </Section>
        </div>
      )}
      {canMetrics && (
        <Section query={adoption}>
          {adoption.data !== undefined && <AdoptionView data={adoption.data} zoneName={zoneName} />}
        </Section>
      )}
      {weekly && permissions.includes('executive_summary.read') && (
        <Section query={overdue}>
          {overdue.data !== undefined && (
            <>
              <OverdueView
                data={{ ...overdue.data, items: [...previousItems, ...overdue.data.items] }}
                zoneName={zoneName}
              />
              {overdue.data.next_cursor !== null && (
                <button
                  type="button"
                  onClick={() => {
                    setPreviousItems((items) => [...items, ...(overdue.data?.items ?? [])]);
                    setCursor(overdue.data?.next_cursor ?? null);
                  }}
                >
                  {text('scr13.next')}
                </button>
              )}
            </>
          )}
        </Section>
      )}
    </>
  );
}

function Selection({ weekly }: { readonly weekly: boolean }) {
  const { principal } = useUiSession();
  const organizationId = principal?.organization.organization_id ?? '';
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr13',
      operation: 'GET /hierarchy',
      scope: organizationId,
      concession,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => metricsPort.hierarchy.read({}, signal),
  });
  const [zoneId, setZoneId] = useState('');
  // VIG-363: el resumen del gerente parte del mes de la semana anterior (las cifras son mensuales).
  const [month, setMonth] = useState(() =>
    formatInTimeZone(metricsClock().now() - (weekly ? WEEK_MS : 0), 'UTC', 'yyyy-MM'),
  );
  const [applied, setApplied] = useState<{
    zoneId: string;
    month: string;
    revision: number;
  } | null>(null);
  const [touched, setTouched] = useState(false);
  // Su inicio llega cargado con la primera zona de su alcance hasta que elija otra cosa.
  const firstZone = hierarchy.data?.plants[0]?.zones[0]?.zone_id;
  const preset = weekly && !touched ? firstZone : undefined;
  const shownZone = preset ?? zoneId;
  const shown = applied ?? (preset === undefined ? null : { zoneId: preset, month, revision: 0 });
  const plant = hierarchy.data?.plants.find((entry) =>
    entry.zones.some((zone) => zone.zone_id === shown?.zoneId),
  );
  const context = {
    organizationId,
    concessionOrganizationId:
      concession === null ? null : (hierarchy.data?.organization.organization_id ?? null),
  };
  return (
    <main className={styles.page}>
      <h1>{text(weekly ? 'scr13.weekly' : 'scr13.title')}</h1>
      <Link to={weekly ? '/metrics' : '/metrics/weekly'}>
        {text(weekly ? 'scr13.title' : 'scr13.weekly')}
      </Link>
      {weekly && <p>{text('scr13.monthly_notice')}</p>}
      <p>{text('scr13.goal')}</p>
      <Section query={hierarchy}>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (shownZone !== '' && /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(month)) {
              setTouched(true);
              setZoneId(shownZone);
              setApplied({ zoneId: shownZone, month, revision: (shown?.revision ?? 0) + 1 });
            }
          }}
        >
          <label>
            {text('scr13.zone')}
            <select
              required
              value={shownZone}
              onChange={(event) => {
                setTouched(true);
                setZoneId(event.target.value);
                setApplied(null);
              }}
            >
              <option value="">{text('scr13.choose_zone')}</option>
              {hierarchy.data?.plants.flatMap((entry) =>
                entry.zones.map((zone) => (
                  <option value={zone.zone_id} key={zone.zone_id}>
                    {entry.name}: {zone.name}
                  </option>
                )),
              )}
            </select>
          </label>
          <label>
            {text('scr13.period')}
            <input
              required
              type="month"
              value={month}
              onChange={(event) => {
                setTouched(true);
                setZoneId(shownZone);
                setMonth(event.target.value);
                setApplied(null);
              }}
            />
          </label>
          <button type="submit">{text('scr13.consult')}</button>
        </form>
      </Section>
      {weekly && (
        <HomeCards
          title="scr13.home.cards"
          cards={[
            { slot: 'SCR-03:integrity', hint: 'scr13.home.card.integrity' },
            { slot: 'SCR-02:*', hint: 'scr13.home.card.providers' },
            { slot: 'SCR-12:*', hint: 'scr13.home.card.monthly' },
          ]}
        />
      )}
      {shown === null || plant === undefined ? (
        <p>{text('scr13.not_requested')}</p>
      ) : (
        <Results
          key={`${shown.zoneId}:${shown.month}:${String(shown.revision)}`}
          plant={plant}
          zoneId={shown.zoneId}
          month={shown.month}
          context={context}
          weekly={weekly}
          permissions={principal?.effective_permissions ?? []}
          concession={concession}
        />
      )}
    </main>
  );
}

export function ProgramPage({ weekly = false }: { readonly weekly?: boolean }) {
  const { principal } = useUiSession();
  return (
    <Selection
      key={`${principal?.organization.organization_id ?? ''}:${principal?.concession_id ?? ''}`}
      weekly={weekly}
    />
  );
}

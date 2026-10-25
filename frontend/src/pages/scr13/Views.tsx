/** RV-17: proyecciones explícitas. Ningún objeto de la API se vuelca al árbol. */
import { MetricsChart } from '@/components/MetricsChart/MetricsChart';
import { parseMetricsSeries } from '@/components/MetricsChart/metrics';
import { CoverageTimeline } from '@/components/CoverageTimeline/CoverageTimeline';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  acceptsOrganization,
  type MetricsContext,
  type MetricSnapshot,
  type Exposure,
  type Adoption,
  type Overdue,
} from '@/ports/scr13Metrics';
import type { CoverageComposite, CoveragePeriod } from '@/state/coverage/compose';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

export function MetricView({
  data,
  zoneName,
  ...context
}: { readonly data: MetricSnapshot; readonly zoneName: string } & MetricsContext) {
  if (!acceptsOrganization(data.organization_id, context)) return <ScreenState state="error" />;
  if (!/^[0-9]{4}-(0[1-9]|1[0-2])$/.test(data.period)) return <ScreenState state="error" />;
  return (
    <section aria-label={zoneName}>
      <h2>
        {zoneName} · {data.period}
      </h2>
      {data.values
        .filter((metric) => metric.key !== 'exposure')
        .map((metric, index) => (
          <section key={index} aria-label={text(`scr13.${metric.key}`)}>
            <h3>{text(`scr13.${metric.key}`)}</h3>
            {metric.status === 'not_computable' || metric.value == null ? (
              <p>{text('scr13.not_computable')}</p>
            ) : (
              <>
                {metric.status === 'partial' && <p>{text('scr13.partial')}</p>}
                <SeriesView
                  title={text(`scr13.${metric.key}`)}
                  valueLabel={text(`scr13.${metric.unit}`)}
                  series={{
                    visualization: 'table',
                    dimensions: ['zone', 'period'],
                    points: [{ zone: zoneName, period: data.period, value: metric.value }],
                  }}
                />
              </>
            )}
          </section>
        ))}
    </section>
  );
}

/** Una serie externa se valida antes de pintar, también en la vista registrada. */
export function SeriesView({
  series,
  title = text('scr13.hours'),
  valueLabel = text('scr13.confirmed'),
}: {
  readonly series: unknown;
  readonly title?: string;
  readonly valueLabel?: string;
}) {
  return <MetricsChart title={title} valueLabel={valueLabel} series={parseMetricsSeries(series)} />;
}

function minute(value: number) {
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
}

export function ExposureView({
  data,
  composite,
  clock,
  timeZone,
  onPeriodChange,
}: {
  readonly data: Exposure;
  readonly composite: CoverageComposite;
  readonly clock: Clock;
  readonly timeZone: string;
  readonly onPeriodChange: (period: CoveragePeriod) => void;
}) {
  // Fallo cerrado: ni una cifra fuera de su cobertura, tampoco durante el cambio de periodo.
  if (
    composite.status !== 'complete' ||
    composite.period.fromMs !== Date.parse(data.period_from) ||
    composite.period.toMs !== Date.parse(data.period_to) ||
    Date.parse(data.coverage_timeline.period.from) !== composite.period.fromMs ||
    Date.parse(data.coverage_timeline.period.to) !== composite.period.toMs
  )
    return <ScreenState state="unavailable" />;
  if (data.status === 'not_computable') return <p>{text('scr13.not_computable')}</p>;
  return (
    <section aria-label={text('scr13.exposure')}>
      <h2>
        {text('scr13.exposure')} · {data.period}
      </h2>
      <p>
        {text(
          data.status === 'partial' || data.observable_coverage_percent < 80
            ? 'scr13.partial'
            : 'scr13.computed',
        )}
      </p>
      <dl>
        <dt>{text('scr13.confirmed')}</dt>
        <dd>{data.confirmed_minutes}</dd>
        <dt>{text('scr13.pending')}</dt>
        <dd>{data.pending_classification_minutes}</dd>
        <dt>{text('scr13.coverage')}</dt>
        <dd>{data.observable_coverage_percent}</dd>
      </dl>
      <fieldset disabled>
        <legend>{text('scr13.period')}</legend>
        <CoverageTimeline
          composite={composite}
          clock={clock}
          timeZone={timeZone}
          onPeriodChange={onPeriodChange}
        />
      </fieldset>
      {/* Se preserva el ancho del cubo del servidor; nunca se reconstruyen horas individuales. */}
      <table>
        <caption>{text('scr13.hours')}</caption>
        <thead>
          <tr>
            <th scope="col">{text('scr13.window')}</th>
            <th scope="col">{text('scr13.confirmed')}</th>
          </tr>
        </thead>
        <tbody>
          {data.hourly_distribution.map((bucket) => (
            <tr key={bucket.start_minute_of_day}>
              <th scope="row">
                {minute(bucket.start_minute_of_day)}–
                {minute(bucket.start_minute_of_day + bucket.width_minutes)}
              </th>
              <td>{bucket.confirmed_minutes}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.coverage_timeline.annotations.length > 0 && (
        <section aria-label={text('scr13.annotations')}>
          <h3>{text('scr13.annotations')}</h3>
          <ul>
            {data.coverage_timeline.annotations.map((note, index) => (
              <li key={index}>
                {text(
                  note.kind === 'gate_revoked'
                    ? 'scr13.gate_revoked'
                    : note.kind === 'acceptance_not_current'
                      ? 'scr13.acceptance_not_current'
                      : 'scr13.annotation',
                )}
                : {note.from}–{note.to}
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}

export function AdoptionView({
  data,
  zoneName,
}: {
  readonly data: Adoption;
  readonly zoneName: string;
}) {
  return (
    <section aria-label={text('scr13.panel_usage')}>
      <h2>{text('scr13.panel_usage')}</h2>
      {data.zones.map((zone) => (
        <section key={zone.zone_id}>
          <h3>{zoneName}</h3>
          <p>
            {text('scr13.silent_days')}: {zone.silent_days ?? text('scr13.no_activity')}
          </p>
          <table>
            <caption>{text('scr13.activity_count')}</caption>
            <thead>
              <tr>
                <th scope="col">{text('scr13.day')}</th>
                <th scope="col">{text('scr13.activity_count')}</th>
              </tr>
            </thead>
            <tbody>
              {zone.days.map((day) => (
                <tr key={day.day}>
                  <th scope="row">{day.day}</th>
                  <td>{day.activity_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </section>
  );
}

export function OverdueView({
  data,
  zoneName,
}: {
  readonly data: Overdue;
  readonly zoneName: string;
}) {
  return (
    <section aria-label={text('scr13.overdue')}>
      <h2>{text('scr13.overdue')}</h2>
      {data.zones.map((zone) => (
        <p key={zone.zone_id}>
          {zoneName}: {text('scr13.overdue_count')} {zone.overdue_count}
        </p>
      ))}
      <table>
        <caption>{text('scr13.overdue_days')}</caption>
        <thead>
          <tr>
            <th scope="col">{text('scr13.zone')}</th>
            <th scope="col">{text('scr13.overdue_days')}</th>
            <th scope="col">{text('scr13.over_30')}</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((item) => (
            <tr key={item.finding_state_id}>
              <th scope="row">{zoneName}</th>
              <td>{item.overdue_days}</td>
              <td>{item.overdue_days > 30 && <strong>{text('scr13.over_30')}</strong>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

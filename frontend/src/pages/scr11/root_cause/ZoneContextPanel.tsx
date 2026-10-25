/**
 * Panel de contexto objetivo de la zona (RV-15; `ZoneObjectiveContext`, BR-LAZ-63): solo hechos
 * medidos que el servidor congeló al iniciar el análisis (historial por familia, reincidencias,
 * estado del nodo y de las señales, familia, tier, estándar y acciones previas). Ningún valor
 * crudo: toda lista cerrada se muestra con su etiqueta.
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { ZoneContext } from '@/ports/scr11RootCause';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import styles from './root_cause.module.css';

const number = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 1 });

function minutes(ms: number): string {
  return number.format(ms / 60_000);
}

function seconds(ms: number | null): string {
  return ms === null ? text('scr11.context_not_measured') : number.format(ms / 1_000);
}

const COVERAGE = [
  ['observable_ms', 'scr11.context_coverage_observable'],
  ['degraded_ms', 'scr11.context_coverage_degraded'],
  ['not_observable_ms', 'scr11.context_coverage_not_observable'],
  ['no_communication_ms', 'scr11.context_coverage_no_communication'],
  ['never_reported_ms', 'scr11.context_coverage_never_reported'],
  ['zone_not_active_ms', 'scr11.context_coverage_zone_not_active'],
] as const;

function None() {
  return <p>{text('scr11.context_none')}</p>;
}

export function ZoneContextPanel({
  context,
  timeZone,
}: {
  readonly context: ZoneContext;
  readonly timeZone: string;
}) {
  const title = text('scr11.context_title');
  return (
    <section className={styles.context} aria-label={title}>
      <h2>{title}</h2>
      <p>{text('scr11.context_intro')}</p>
      <dl className={styles.facts}>
        <dt>{text('scr11.context_family')}</dt>
        <dd data-part="family">{label('predicate_family', context.family)}</dd>
        <dt>{text('scr11.context_tier')}</dt>
        <dd data-part="tier">{label('evidence_tier', context.tier)}</dd>
        <dt>{text('scr11.context_standard')}</dt>
        <dd data-part="standard">
          {context.standard_title_es} ({text('scr11.context_version')}{' '}
          {String(context.standard_version)})
        </dd>
        <dt>{text('scr11.context_observability')}</dt>
        <dd data-part="observability">
          {label('observability_state', context.observability_state_at_event)}
        </dd>
        <dt>{text('scr11.context_period')}</dt>
        <dd data-part="period">
          <TimestampWithOffset instant={context.period.from} timeZone={timeZone} />{' '}
          {text('scr11.context_period_to')}{' '}
          <TimestampWithOffset instant={context.period.to} timeZone={timeZone} />
        </dd>
        <dt>{text('scr11.context_recurrences')}</dt>
        <dd data-part="recurrences">{String(context.recurrences_in_period)}</dd>
      </dl>
      <h3>{text('scr11.context_standard_text')}</h3>
      <blockquote className={styles.quote} data-part="standard-text">
        {context.standard_text}
      </blockquote>

      <h3>{text('scr11.context_coverage')}</h3>
      <dl className={styles.facts} data-part="coverage">
        {COVERAGE.map(([field, key]) => (
          <div key={field} className={styles.pair}>
            <dt>{text(key)}</dt>
            <dd>{minutes(context.coverage_summary_period[field])}</dd>
          </div>
        ))}
      </dl>

      <h3>{text('scr11.context_nodes')}</h3>
      {context.node_state.length === 0 ? (
        <None />
      ) : (
        <ul aria-label={text('scr11.context_nodes')}>
          {context.node_state.map((node, index) => (
            <li key={index} data-part="node">
              {label('communication_state', node.communication_state)}
              {' · '}
              {node.last_heartbeat_at === null ? (
                text('scr11.context_no_heartbeat')
              ) : (
                <>
                  {text('scr11.context_last_heartbeat')}{' '}
                  <TimestampWithOffset instant={node.last_heartbeat_at} timeZone={timeZone} />
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>{text('scr11.context_signals')}</h3>
      {context.signals_by_role.length === 0 ? (
        <None />
      ) : (
        <ul aria-label={text('scr11.context_signals')}>
          {context.signals_by_role.map((signal, index) => (
            <li key={index} data-part="signal">
              {label('signal_role', signal.role)}: {label('signal_value', signal.value)}
              {signal.valid ? null : ` (${text('scr11.context_signal_invalid')})`}
            </li>
          ))}
        </ul>
      )}

      <h3>{text('scr11.context_durations')}</h3>
      <dl className={styles.facts}>
        <dt>{text('scr11.context_episode')}</dt>
        <dd data-part="episode-duration">{seconds(context.episode_durations.episode_ms)}</dd>
        <dt>{text('scr11.context_condition')}</dt>
        <dd data-part="condition-duration">{seconds(context.episode_durations.condition_ms)}</dd>
      </dl>

      <h3>{text('scr11.context_history')}</h3>
      {context.finding_counts_by_family_in_period.length === 0 ? (
        <None />
      ) : (
        <ul aria-label={text('scr11.context_history')}>
          {context.finding_counts_by_family_in_period.map((entry, index) => (
            <li key={index} data-part="family-count">
              {label('predicate_family', entry.family)}: {String(entry.count)}
            </li>
          ))}
        </ul>
      )}

      <h3>{text('scr11.context_previous_actions')}</h3>
      {context.previous_actions.length === 0 ? (
        <None />
      ) : (
        <ul aria-label={text('scr11.context_previous_actions')}>
          {context.previous_actions.map((action, index) => (
            <li key={index} data-part="previous-action">
              {label('action_type', action.action_type)} ·{' '}
              {label('finding_lifecycle_state', action.outcome)}
              {action.closed_at == null ? null : (
                <>
                  {' · '}
                  {text('scr11.context_closed_at')}{' '}
                  <TimestampWithOffset instant={action.closed_at} timeZone={timeZone} />
                </>
              )}
              {action.recurrence_after_days == null
                ? null
                : ` · ${text('scr11.context_recurrence_after')}: ${String(
                    action.recurrence_after_days,
                  )}`}
            </li>
          ))}
        </ul>
      )}

      <h3>{text('scr11.context_previous_causes')}</h3>
      {context.previous_cause_categories.length === 0 ? (
        <None />
      ) : (
        <ul aria-label={text('scr11.context_previous_causes')}>
          {context.previous_cause_categories.map((entry, index) => (
            <li key={index} data-part="previous-cause">
              {label('cause_category', entry.category)}: {String(entry.count)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

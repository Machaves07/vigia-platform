/** Trabajo acotado: la selección de 50 zonas es única para compuertas y cobertura. */
import { useState } from 'react';
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import { ObservabilityBadge } from '@/components/ObservabilityBadge/ObservabilityBadge';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  findingsClock,
  findingsPort,
  gateDisplay,
  readGates,
  type Gate,
  type Plant,
  type Coverage,
  type Zone,
} from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { Refresh, Result, useFindingsContext } from '../shared';
import styles from './home.module.css';

export function ZoneCard({
  zone,
  gate,
  regression,
  coverage,
  timeZone,
}: {
  readonly zone: Zone;
  readonly gate: Gate | null;
  readonly regression: string;
  readonly coverage: Coverage | null;
  readonly timeZone: string;
}) {
  const display = gate === null ? { state: 'unknown' } : gateDisplay(gate, regression);
  return (
    <article aria-label={zone.name} className={styles.card}>
      <h3>{zone.name}</h3>
      <GateStateChip gate={display} timeZone={timeZone} />
      {display.state === 'productive' && (
        <>
          <ObservabilityBadge
            state={coverage?.state ?? 'unknown'}
            {...(coverage === null
              ? {}
              : { since: { instant: coverage.instant, timeZone }, cause: coverage.causes[0] })}
          />
          {coverage === null && <p>{text('scr08.home.coverage_unavailable')}</p>}
        </>
      )}
    </article>
  );
}

export function HomeZones({
  context,
}: {
  readonly context: ReturnType<typeof useFindingsContext>;
}) {
  const zones = context.plants.flatMap((plant) => plant.zones);
  const visible = zones.slice(0, 50);
  const rest = zones.slice(visible.length);
  const ids = visible.map((zone) => zone.zone_id);
  const coverageScope = `${context.scope}:${context.concession ?? ''}:${ids.join(',')}`;
  const [intent, setIntent] = useState<{ scope: string; instant: string } | null>(null);
  const instant = intent?.scope === coverageScope ? intent.instant : null;
  const canReadGates = context.permissions.includes('catalog.read');
  const gates = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation: 'GET /zones/{zone_id}/gates',
      scope: context.scope,
      concession: context.concession,
      params: { zones: ids },
    }),
    policy: policyFor('GET /zones/{zone_id}/gates'),
    enabled: canReadGates && context.hierarchy.status === 'success',
    read: ({ signal }) => readGates(ids, signal),
  });
  const coverage = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation: 'GET /zones/{zone_id}/coverage/at',
      scope: context.scope,
      concession: context.concession,
      params: { zones: ids, instant },
    }),
    policy: policyFor('GET /zones/{zone_id}/coverage/at'),
    enabled: instant !== null,
    read: ({ signal }) =>
      Promise.all(
        visible.map((zone) =>
          findingsPort.coverage.read(
            { path: { zone_id: zone.zone_id }, query: { instant: instant ?? '' } },
            signal,
          ),
        ),
      ),
  });
  return (
    <section aria-label={text('scr08.home.zones')}>
      <h2>{text('scr08.home.zones')}</h2>
      <Refresh query={context.hierarchy} operation="GET /hierarchy" />
      <Result query={context.hierarchy} empty={zones.length === 0}>
        {!canReadGates ? (
          <ScreenState state="unavailable" />
        ) : (
          <>
            <Refresh query={gates} operation="GET /zones/{zone_id}/gates" />
            <Result query={gates}>
              <button
                type="button"
                disabled={coverage.isFetching || !context.permissions.includes('coverage.read')}
                onClick={() => {
                  const now = new Date(findingsClock().now()).toISOString();
                  if (now === instant) void coverage.refresh();
                  else setIntent({ scope: coverageScope, instant: now });
                }}
              >
                {text('scr08.home.load_coverage')}
              </button>
              {instant !== null && (
                <>
                  <Refresh query={coverage} operation="GET /zones/{zone_id}/coverage/at" />
                  <Result query={coverage}>
                    <p>{text('scr08.home.coverage_at')}</p>
                  </Result>
                </>
              )}
              <div className={styles.zones}>
                {visible.map((zone) => {
                  const status = gates.data?.find((value) => value.zone_id === zone.zone_id);
                  return (
                    <ZoneCard
                      key={zone.zone_id}
                      zone={zone}
                      gate={status?.gate ?? null}
                      regression={status?.regression ?? 'unknown'}
                      coverage={
                        coverage.data?.find((value) => value.zone_id === zone.zone_id) ?? null
                      }
                      timeZone={
                        context.plants.find((plant) => plant.plant_id === zone.plant_id)
                          ?.timezone ?? ''
                      }
                    />
                  );
                })}
              </div>
            </Result>
          </>
        )}
        <GroupedZones zones={rest} plants={context.plants} />
      </Result>
    </section>
  );
}

function GroupedZones({
  zones,
  plants,
}: {
  readonly zones: readonly Zone[];
  readonly plants: readonly Plant[];
}) {
  return (
    <>
      {plants.map((plant) => {
        const grouped = zones.filter((zone) => zone.plant_id === plant.plant_id);
        return grouped.length === 0 ? null : (
          <details key={plant.plant_id} data-part="grouped-zones">
            <summary>
              {text('scr08.home.grouped')}: {plant.name}
            </summary>
            <ul>
              {grouped.map((zone) => (
                <li key={zone.zone_id}>{zone.name}</li>
              ))}
            </ul>
            <p>{text('scr08.home.grouped_hint')}</p>
          </details>
        );
      })}
    </>
  );
}

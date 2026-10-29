/**
 * Franja de cobertura del inicio (sistema visual v1; S3-04 y S3-07 de VIG-365): una tarjeta por
 * zona, hasta 50, con su compuerta y, si es productiva, su semáforo. La cobertura llega cargada:
 * abrir el inicio es la acción explícita que pide la lectura auditada, una vez por apertura, como el
 * detalle; después solo «Actualizar» de la cabecera la repite, en el instante de pulsarlo. Nunca se
 * pinta «Desconocido» mientras llega: la tarjeta dice que se está consultando. La leyenda
 * «Observable no significa zona despejada» va una vez, en la franja, si alguna zona es observable.
 */
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import {
  ObservabilityBadge,
  ObservabilityLegend,
} from '@/components/ObservabilityBadge/ObservabilityBadge';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  gateDisplay,
  type Coverage,
  type Gate,
  type Plant,
  type Zone,
} from '@/ports/scr08Findings';
import { text } from '@/texts';
import { Result } from '../shared';
import styles from './home.module.css';

/** Zonas con tarjeta en el inicio: la selección es única para compuertas y cobertura. */
export const HOME_ZONES = 50;

export function ZoneCard({
  zone,
  gate,
  regression,
  coverage,
  timeZone,
  loading = false,
}: {
  readonly zone: Zone;
  readonly gate: Gate | null;
  readonly regression: string;
  readonly coverage: Coverage | null;
  readonly timeZone: string;
  /** La cobertura de la zona está en camino. */
  readonly loading?: boolean;
}) {
  const display = gate === null ? { state: 'unknown' } : gateDisplay(gate, regression);
  return (
    <article aria-label={zone.name} className={styles.card}>
      <h3>{zone.name}</h3>
      {display.state !== 'productive' ? (
        <GateStateChip gate={display} timeZone={timeZone} />
      ) : coverage !== null ? (
        <ObservabilityBadge
          state={coverage.state}
          since={{ instant: coverage.instant, timeZone }}
          inTable
          {...(coverage.causes[0] === undefined ? {} : { cause: coverage.causes[0] })}
        />
      ) : (
        loading && <p data-part="coverage_loading">{text('scr08.home.coverage_loading')}</p>
      )}
    </article>
  );
}

export interface ZoneStatus {
  readonly zone_id: string;
  readonly gate: Gate;
  readonly regression: string;
}

export function HomeZones({
  plants,
  gates,
  coverage,
}: {
  readonly plants: readonly Plant[];
  /** Compuertas de las zonas con tarjeta. */
  readonly gates: {
    readonly data: readonly ZoneStatus[] | undefined;
    readonly status: string;
    readonly error: Error | null;
    refresh(): Promise<void>;
  };
  /** Cobertura de las zonas con tarjeta; `null` sin `coverage.read`. */
  readonly coverage: {
    readonly data: readonly Coverage[] | undefined;
    readonly status: string;
    readonly error: Error | null;
    readonly isFetching: boolean;
    refresh(): Promise<void>;
  } | null;
}) {
  const zones = plants.flatMap((plant) => plant.zones);
  const visible = zones.slice(0, HOME_ZONES);
  const rest = zones.slice(visible.length);
  const observable = coverage?.data?.some((value) => value.state === 'observable') ?? false;
  return (
    <section aria-label={text('scr08.home.coverage')} className={styles.block}>
      <h2>{text('scr08.home.coverage')}</h2>
      <p className={styles.hint}>{text('scr08.home.coverage_hint')}</p>
      {zones.length === 0 ? (
        <ScreenState state="empty" message={text('scr08.inbox.empty')} />
      ) : (
        <Result query={gates}>
          {coverage?.error != null && (
            <ScreenState
              state="error"
              onRetry={() => {
                void coverage.refresh();
              }}
            />
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
                  coverage={coverage?.data?.find((value) => value.zone_id === zone.zone_id) ?? null}
                  loading={coverage !== null && coverage.error === null}
                  timeZone={
                    plants.find((plant) => plant.plant_id === zone.plant_id)?.timezone ?? ''
                  }
                />
              );
            })}
          </div>
          {observable && <ObservabilityLegend />}
        </Result>
      )}
      <GroupedZones zones={rest} plants={plants} />
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

/** RV-10: proyección explícita; nunca se recorre ni serializa un objeto recibido. */
import { Link } from 'react-router';
import {
  ObservabilityBadge,
  ObservabilityLegend,
} from '@/components/ObservabilityBadge/ObservabilityBadge';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { zoneName } from '@/components/TimestampWithOffset/format';
import type { FleetNode, FleetWarning } from '@/ports/scr07Fleet';
import { TechnicalDetails } from '@/pages/shared/TechnicalDetails';
import { text } from '@/texts';
import { formatDuration } from '@/texts/duration';
import { VirtualFleetTable, type FleetRow } from './VirtualFleetTable';

export const WARNINGS: readonly FleetWarning[] = [
  'node_mute',
  'queue_over_threshold',
  'clock_drift',
  'version_retiring',
  'simulated_adapter_in_productive',
  'certificate_expiring',
  'camera_below_min_fps',
  'orphan_clips_growing',
];
export const COMMUNICATION = ['unknown', 'reachable', 'mute'] as const;
export function warningLabel(warning: string): string {
  return WARNINGS.some((known) => known === warning)
    ? text(`scr07.alarm_warning.${warning}`)
    : text('scr07.unknown');
}
function updateLabel(value: string | null): string {
  return value !== null && ['applied', 'reverted', 'failed'].includes(value)
    ? text(`scr07.update.${value}`)
    : text('scr07.unknown');
}
export function FleetTime({
  instant,
  timeZone,
  zone = 'inline',
}: {
  readonly instant: string | null | undefined;
  readonly timeZone: string;
  /** `'header'` cuando la tabla nombra la zona una sola vez, en la cabecera (VIG-366). */
  readonly zone?: 'inline' | 'header';
}) {
  return instant == null ? (
    text('scr07.unknown')
  ) : (
    <TimestampWithOffset instant={instant} timeZone={timeZone} zone={zone} />
  );
}

/** Columnas con horas: con una sola zona horaria, la nombran en su cabecera. */
const TIMED_HEADINGS: ReadonlySet<string> = new Set(['scr07.communication', 'scr07.health']);

/** La zona común a todas las plantas de la tabla, o `null` si hay más de una o falta alguna. */
function sharedZone(nodes: readonly FleetNode[], plants: readonly FleetPlant[]): string | null {
  const zones = new Set(
    nodes.map((node) => plants.find((plant) => plant.plant_id === node.plant_id)?.timezone ?? ''),
  );
  const [only] = zones;
  return zones.size === 1 && only !== undefined && only !== '' && zoneName(only) !== null
    ? only
    : null;
}

export interface FleetPlant {
  readonly plant_id: string;
  readonly name: string;
  readonly timezone: string;
  /** Zonas de la planta, para nombrarlas en vez de mostrar su identificador (VIG-366). */
  readonly zones?: readonly { readonly zone_id: string; readonly name: string }[];
}

/** Desviación del reloj del nodo con signo, en segundos (VIG-366: nunca en «ms»). */
function clockOffset(offsetMs: number | null | undefined): string {
  if (offsetMs === null || offsetMs === undefined) return text('scr07.unknown');
  const magnitude = formatDuration(Math.abs(offsetMs));
  if (magnitude === null) return text('scr07.unknown');
  return offsetMs === 0 ? magnitude : `${offsetMs < 0 ? '−' : '+'}${magnitude}`;
}
export function FleetView({
  nodes,
  plants,
}: {
  readonly nodes: readonly FleetNode[];
  readonly plants: readonly FleetPlant[];
}) {
  // Con una sola zona horaria, la zona va una vez en la cabecera y no en cada hora (VIG-366).
  const tableZone = sharedZone(nodes, plants);
  const zone = tableZone === null ? 'inline' : 'header';
  const rows: FleetRow[] = nodes.flatMap((node) => {
    const plant = plants.find((entry) => entry.plant_id === node.plant_id);
    // No se inventa una zona horaria mientras la jerarquía no esté disponible.
    const timeZone = plant?.timezone ?? '';
    const nodeRow: FleetRow = {
      id: node.node_id,
      cells: [
        <>
          <Link to={`/fleet/nodes/${node.node_id}`}>{node.code}</Link>
          <p>{plant?.name ?? text('scr07.unknown')}</p>
          <Link to={`/plants/${node.plant_id}/fleet-thresholds`}>{text('scr07.thresholds')}</Link>
          <TechnicalDetails
            items={[
              { name: text('common.identifier'), value: node.node_id },
              ...(node.live_view_local_url === null
                ? []
                : [{ name: text('scr07.live_url'), value: node.live_view_local_url }]),
            ]}
          />
        </>,
        <>
          {text('scr07.versions')}:{' '}
          {[node.software_version, node.contract_version, node.model_version]
            .map((version) => version ?? text('scr07.unknown'))
            .join(' / ')}
          <p>
            {text('scr07.target')}: {node.target_version ?? text('scr07.unknown')}
          </p>
          <p>
            {text('scr07.update')}: {updateLabel(node.last_update_result)}
          </p>
        </>,
        <>
          {text(`scr07.communication.${node.communication_state}`)}
          <p>
            {text('scr07.since')}:{' '}
            <FleetTime instant={node.since} timeZone={timeZone} zone={zone} />
          </p>
          <p>
            {text('scr07.heartbeat')}:{' '}
            <FleetTime instant={node.last_heartbeat_at} timeZone={timeZone} zone={zone} />
          </p>
        </>,
        <>
          {text('scr07.queue')}: {node.local_queue?.pending ?? text('scr07.unknown')}
          <p>
            {text('scr07.oldest')}:{' '}
            <FleetTime
              instant={node.local_queue?.oldest_pending_at}
              timeZone={timeZone}
              zone={zone}
            />
          </p>
          <p>
            {text('scr07.dead_letter')}:{' '}
            {node.local_queue?.dead_letter.length ?? text('scr07.unknown')}
          </p>
          <p>
            {text('scr07.clock')}: {clockOffset(node.clock?.offset_ms)}
          </p>
        </>,
        <>
          <ul>
            {node.warnings.map((warning) => (
              <li key={warning}>{warningLabel(warning)}</li>
            ))}
          </ul>
        </>,
        <>
          {/* La dirección local (IP interna) va plegada en los datos técnicos del nodo. */}
          {text(
            node.live_view_local_url === null ? 'scr07.live_unavailable' : 'scr07.live_available',
          )}
        </>,
      ],
    };
    const cameras: FleetRow[] = node.cameras.map((camera) => ({
      id: `${node.node_id}:camera:${camera.camera_id}`,
      cells: [
        <>
          {text('scr07.camera')}: {camera.code ?? text('scr07.camera_without_code')}
          <p>{node.code}</p>
        </>,
        <>
          {text('scr07.fps')}: {camera.measured_fps} / {camera.declared_min_fps}
        </>,
        <>{text(camera.connected ? 'scr07.connected' : 'scr07.disconnected')}</>,
        <ObservabilityBadge state={camera.observability_state} inTable />,
        null,
        null,
      ],
    }));
    const zones: FleetRow[] = node.zones.map((zoneId) => {
      const zone = node.zone_states.find((entry) => entry.zone_id === zoneId);
      return {
        id: `${node.node_id}:zone:${zoneId}`,
        cells: [
          <>
            {text('scr07.zone')}:{' '}
            {plant?.zones?.find((entry) => entry.zone_id === zoneId)?.name ?? text('scr07.unknown')}
            <p>{node.code}</p>
          </>,
          <>
            {text('scr07.catalog')}: {zone?.catalog_version ?? text('scr07.unknown')}
          </>,
          null,
          <>
            <ObservabilityBadge state={zone?.observability_state ?? 'unknown'} inTable />
            {text(
              zone === undefined
                ? 'scr07.unknown'
                : zone.coverage_ok
                  ? 'scr07.coverage_met'
                  : 'scr07.coverage_not_met',
            )}
          </>,
          null,
          null,
        ],
      };
    });
    return [nodeRow, ...cameras, ...zones];
  });
  // La leyenda del semáforo, una vez por tabla y solo si alguna fila es «Observable» (VIG-365).
  const observable = nodes.some(
    (node) =>
      node.cameras.some((camera) => camera.observability_state === 'observable') ||
      node.zone_states.some((zone) => zone.observability_state === 'observable'),
  );
  return (
    <>
      <VirtualFleetTable
        rows={rows}
        caption={text('scr07.title')}
        headings={[
          'scr07.resource',
          'scr07.version_rate',
          'scr07.communication',
          'scr07.health',
          'scr07.warning',
          'scr07.live_url',
        ].map((heading) =>
          tableZone !== null && TIMED_HEADINGS.has(heading)
            ? `${text(heading)} (${zoneName(tableZone) ?? ''})`
            : text(heading),
        )}
      />
      {observable && <ObservabilityLegend />}
    </>
  );
}

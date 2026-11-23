/**
 * SCR-08, grupo `history`: «Histórico de lo cerrado» (TASK-586; O-11; BR-APP-53, 57; UI-14 y
 * UI-20). Lo que el equipo ya cerró o descartó, aparte de la bandeja, que sigue siendo la cola de
 * lo pendiente.
 *
 * Una `KeysetTable` sobre `GET /loop/closed-findings` (desde la proyección, sin auditoría de
 * consulta): 50 filas por página con el cursor opaco del servidor, filtros de BR-LAZ-02 aplicados
 * en bloque con el botón (zona, familia, tier, estado y periodo, nunca por tecla) y «Actualizar»
 * a demanda. Cada fila abre la ficha de vida (`/findings/:finding_state_id/timeline`), que es la
 * lectura auditada. Las horas van en la zona horaria de la planta y, en zona unipersonal,
 * redondeadas a su ventana tal como las da el servidor (BR-LAZ-06).
 */
import { useCallback, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { KeysetTable, type KeysetFilterField } from '@/components/KeysetTable/KeysetTable';
import type { KeysetFilters, KeysetRequest } from '@/components/KeysetTable/keyset';
import { ScreenState } from '@/pages/shared/ScreenState';
import { reasonList } from '@/ports/scr08Detail';
import type { Plant } from '@/ports/scr08Findings';
import {
  HISTORY_STATES,
  historyClock,
  readClosedPage,
  type ClosedFinding,
  type ClosedFindingsPage,
  type HistoryFilters,
} from '@/ports/scr08History';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { FAMILIES, TIERS, findingLabel } from '../inbox/FindingsView';
import { dayEnd, dayStart, filterTimeZone, FindingTime, useFindingsContext } from '../shared';
import styles from '../inbox/inbox.module.css';

const PARTITION_NOTICES: readonly string[] = ['projection_rebuilding', 'projection_diverged'];

/** La categoría de la clasificación vigente con la etiqueta de la lista de su resultado. */
function reasonText(finding: ClosedFinding): string {
  if (finding.reason_category === null || finding.classification_outcome === null)
    return text('scr08.history.no_reason');
  const list = reasonList(finding.classification_outcome);
  return list === null ? text('scr08.inbox.unknown') : label(list, finding.reason_category);
}

function yesNo(value: boolean): string {
  return text(value ? 'scr08.history.yes' : 'scr08.history.no');
}

/**
 * Los filtros aplicados en la forma del servidor; `null` si una fecha no es válida. Los días se
 * cuentan en la hora de la planta de la zona elegida (S3-11).
 */
function historyFilters(applied: KeysetFilters, plants: readonly Plant[]): HistoryFilters | null {
  const zone = applied.zone ?? '';
  const timeZone = filterTimeZone(plants, zone === '' ? null : zone);
  const family = FAMILIES.find((value) => value === applied.family);
  const tier = TIERS.find((value) => value === applied.tier);
  const state = HISTORY_STATES.find((value) => value === applied.state);
  const from = applied.from ?? '';
  const to = applied.to ?? '';
  const start = from === '' ? null : dayStart(from, timeZone);
  const end = to === '' ? null : dayEnd(to, timeZone);
  if ((from !== '' && start === null) || (to !== '' && end === null)) return null;
  return {
    ...(zone === '' ? {} : { zone_id: [zone] }),
    ...(family === undefined ? {} : { family }),
    ...(tier === undefined ? {} : { tier }),
    ...(state === undefined ? {} : { lifecycle_state: [state] }),
    ...(start === null ? {} : { from: start.toISOString() }),
    ...(end === null ? {} : { to: end.toISOString() }),
  };
}

function HistoryTable({ plants }: { readonly plants: readonly Plant[] }) {
  const navigate = useNavigate();
  const [partitions, setPartitions] = useState<ClosedFindingsPage['partitions']>([]);
  const [invalidPeriod, setInvalidPeriod] = useState(false);
  const fetchPage = useCallback(
    async (request: KeysetRequest, signal: AbortSignal) => {
      const filters = historyFilters(request.filters, plants);
      setInvalidPeriod(filters === null);
      if (filters === null) return { items: [], nextCursor: null };
      const page = await readClosedPage(
        filters,
        request.cursor?.cursor ?? null,
        request.pageSize,
        signal,
      );
      if (request.cursor === null) setPartitions(page.partitions);
      return {
        items: page.items,
        nextCursor: page.next_cursor === null ? null : { cursor: page.next_cursor },
      };
    },
    [plants],
  );
  const zones = useMemo(() => plants.flatMap((plant) => plant.zones), [plants]);
  const filterFields = useMemo<readonly KeysetFilterField[]>(
    () => [
      {
        name: 'zone',
        label: text('scr08.history.zone'),
        kind: 'select',
        options: zones.map((zone) => ({ value: zone.zone_id, label: zone.name })),
      },
      ...(['family', 'tier', 'state'] as const).map((kind): KeysetFilterField => ({
        name: kind,
        label: text(`scr08.history.${kind}`),
        kind: 'select',
        options: (kind === 'family' ? FAMILIES : kind === 'tier' ? TIERS : HISTORY_STATES).map(
          (value) => ({ value, label: findingLabel(kind, value) }),
        ),
      })),
      { name: 'from', label: text('scr08.history.from'), kind: 'date' },
      { name: 'to', label: text('scr08.history.to'), kind: 'date' },
    ],
    [zones],
  );
  const columns = useMemo(() => {
    const zoneName = (id: string) =>
      zones.find((zone) => zone.zone_id === id)?.name ?? text('scr08.inbox.unknown');
    const timeZone = (finding: ClosedFinding) =>
      plants.find((plant) => plant.plant_id === finding.plant_id)?.timezone ?? 'UTC';
    return [
      {
        id: 'zone',
        header: text('scr08.history.zone'),
        cell: (finding: ClosedFinding) => (
          <Link
            to={`/findings/${finding.finding_state_id}/timeline`}
            data-part="timeline_link"
            tabIndex={-1}
          >
            {zoneName(finding.zone_id)}
          </Link>
        ),
      },
      {
        id: 'family',
        header: text('scr08.history.family'),
        cell: (finding: ClosedFinding) => findingLabel('family', finding.family),
      },
      {
        id: 'tier',
        header: text('scr08.history.tier'),
        cell: (finding: ClosedFinding) => findingLabel('tier', finding.tier),
      },
      {
        id: 'state',
        header: text('scr08.history.state'),
        cell: (finding: ClosedFinding) => (
          <span data-part="state" data-value={finding.lifecycle_state}>
            {findingLabel('state', finding.lifecycle_state)}
          </span>
        ),
      },
      {
        id: 'occurred',
        header: text('scr08.history.occurred'),
        cell: (finding: ClosedFinding) => (
          <>
            <FindingTime instant={finding.occurred_at} timeZone={timeZone(finding)} />
            {finding.time_aggregated && (
              <p data-part="aggregated">{text('scr08.history.aggregated')}</p>
            )}
          </>
        ),
      },
      {
        id: 'reason',
        header: text('scr08.history.reason'),
        cell: (finding: ClosedFinding) => reasonText(finding),
      },
      {
        id: 'action',
        header: text('scr08.history.action'),
        cell: (finding: ClosedFinding) => yesNo(finding.has_action),
      },
      {
        id: 'root_cause',
        header: text('scr08.history.root_cause'),
        cell: (finding: ClosedFinding) => yesNo(finding.has_signed_root_cause),
      },
    ];
  }, [plants, zones]);
  return (
    <>
      {partitions.length > 0 && (
        <section
          aria-label={text('scr08.history.partition_notices')}
          className={styles.notices}
          data-part="partition_notices"
        >
          <h2>{text('scr08.history.partition_notices')}</h2>
          <ul>
            {partitions.map((partition) => (
              <li key={partition.plant_id}>
                {plants.find((plant) => plant.plant_id === partition.plant_id)?.name ??
                  partition.plant_id}
                {': '}
                {text(
                  PARTITION_NOTICES.includes(partition.notice)
                    ? `scr08.inbox.notice.${partition.notice}`
                    : 'scr08.inbox.unknown',
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {invalidPeriod && <p role="alert">{text('scr08.history.invalid_period')}</p>}
      <KeysetTable<ClosedFinding>
        caption={text('scr08.history.caption')}
        clock={historyClock()}
        fetchPage={fetchPage}
        rowKey={(finding) => finding.finding_state_id}
        columns={columns}
        filterFields={filterFields}
        emptyText={text('scr08.history.empty')}
        onRowActivate={(finding) => {
          void navigate(`/findings/${finding.finding_state_id}/timeline`);
        }}
        cardsOnTablet
      />
    </>
  );
}

export default function Page() {
  const context = useFindingsContext();
  return (
    <main>
      <nav aria-label={text('scr08.history.navigation')}>
        <Link to="/findings">{text('scr08.history.inbox')}</Link>
      </nav>
      <header className={styles.header}>
        <h1>{text('scr08.history.title')}</h1>
      </header>
      <p>{text('scr08.history.intro')}</p>
      {/* Sin jerarquía, la lista igual se sirve: las zonas salen como «desconocida». */}
      {context.hierarchy.data === undefined && context.hierarchy.error === null ? (
        <ScreenState state="loading" />
      ) : (
        <HistoryTable plants={context.plants} />
      )}
    </main>
  );
}

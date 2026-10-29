/**
 * Inicio del coordinador SST (sistema visual v1, «Coordinador · Inicio»; VIG-365, S3-02 a S3-10):
 * lo urgente primero y una sola actualización, en la cabecera.
 *
 * 1. «Exige acción hoy»: tres tarjetas (vencidas, detecciones por revisar y lo que vence esta
 *    semana) y la tabla de vencidas, siempre visible, con «Abrir lazo» por fila.
 * 2. La franja de cobertura, una tarjeta por zona, cargada al abrir (`HomeZones`).
 * 3. Los 5 hallazgos nuevos más recientes, cada uno con su siguiente paso, y «Abrir la bandeja
 *    completa (N)».
 *
 * Las secciones salen de las claves de la sesión (BR-APP-01); nunca hay contadores de personas.
 * Los recuentos son de hallazgos en zonas productivas (`homeSummary`) y, si la bandeja tiene más
 * de lo leído, lo dicen.
 */
import { useState } from 'react';
import { Link } from 'react-router';
import { StatusChip } from '@/components/StatusChip/StatusChip';
import { canTake, NextStepLink } from '@/pages/shared/NextStepPanel';
import { PageRefresh } from '@/pages/shared/PageRefresh';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  findingsClock,
  findingsPort,
  readGates,
  readInbox,
  type Finding,
  type Plant,
} from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery, type ReadOperation } from '@/state/query';
import { text } from '@/texts';
import { FindingTime, Result, useFindingsContext } from '../shared';
import { findingLabel } from '../inbox/FindingsView';
import { HOME_ZONES, HomeZones } from './HomeZones';
import { HOME_PAGES, homeSummary, type HomeSummary } from './summary';
import styles from './home.module.css';

const INBOX: ReadOperation = 'GET /loop/findings';
const GATES: ReadOperation = 'GET /zones/{zone_id}/gates';
const COVERAGE: ReadOperation = 'GET /zones/{zone_id}/coverage/at';
const HIERARCHY: ReadOperation = 'GET /hierarchy';

/** Vencidas que la tabla del inicio muestra; el resto, en «Lazos de cierre». */
const OVERDUE_SHOWN = 10;

/** Número de apertura: cada montaje del inicio es una apertura con su propia lectura auditada. */
let openings = 0;

type Context = ReturnType<typeof useFindingsContext>;

function zoneName(plants: readonly Plant[], zoneId: string): string {
  for (const plant of plants)
    for (const zone of plant.zones) if (zone.zone_id === zoneId) return zone.name;
  return text('finding_title.unknown_zone');
}

function timeZoneOf(plants: readonly Plant[], plantId: string): string {
  return plants.find((plant) => plant.plant_id === plantId)?.timezone ?? 'UTC';
}

/** Un recuento: «Más de N» si la bandeja tiene más de lo leído. */
function Count({ value, partial }: { readonly value: number; readonly partial: boolean }) {
  return (
    <strong className={styles.count} data-part="count" data-value={value}>
      {partial && value > 0 ? `${text('scr08.home.more_than')} ${String(value)}` : value}
    </strong>
  );
}

function UrgentCards({
  summary,
  canQueue,
}: {
  readonly summary: HomeSummary;
  readonly canQueue: boolean;
}) {
  const cards = [
    {
      id: 'overdue',
      value: summary.overdue.length,
      to: '/findings/loops',
      link: 'scr08.home.card.open_overdue',
    },
    ...(canQueue
      ? [
          {
            id: 'review',
            value: summary.inReview.length,
            to: '/review-queue',
            link: 'scr08.home.card.open_queue',
          },
        ]
      : []),
    {
      id: 'due_week',
      value: summary.dueThisWeek.length,
      to: '/findings/loops',
      link: 'scr08.home.card.open_loops',
    },
  ];
  return (
    <ul className={styles.cards}>
      {cards.map((card) => (
        <li
          key={card.id}
          className={styles.urgentCard}
          data-card={card.id}
          data-alert={card.id === 'overdue' && card.value > 0 ? 'true' : undefined}
        >
          <h3>{text(`scr08.home.card.${card.id}`)}</h3>
          <Count value={card.value} partial={summary.partial} />
          <p className={styles.hint}>{text(`scr08.home.card.${card.id}_hint`)}</p>
          <Link to={card.to}>{text(card.link)}</Link>
        </li>
      ))}
    </ul>
  );
}

function Heading({ keys }: { readonly keys: readonly string[] }) {
  return (
    <thead>
      <tr>
        {keys.map((key) => (
          <th scope="col" key={key} className={key === 'next' ? styles.sticky : undefined}>
            {text(`scr08.home.column.${key}`)}
          </th>
        ))}
      </tr>
    </thead>
  );
}

function OverdueTable({
  findings,
  plants,
  now,
}: {
  readonly findings: readonly Finding[];
  readonly plants: readonly Plant[];
  readonly now: number;
}) {
  const all = [...findings].sort((a, b) => b.overdue_days - a.overdue_days);
  const sorted = all.slice(0, OVERDUE_SHOWN);
  return (
    <section aria-label={text('scr08.home.overdue_table')} className={styles.block}>
      <h2>{text('scr08.home.overdue_table')}</h2>
      {sorted.length === 0 ? (
        <p data-part="overdue_empty">{text('scr08.home.overdue_empty')}</p>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <Heading keys={['zone', 'family', 'state', 'due', 'next']} />
            <tbody>
              {sorted.map((finding) => (
                <tr key={finding.finding_state_id} data-finding={finding.finding_state_id}>
                  <th scope="row">{zoneName(plants, finding.zone_id)}</th>
                  <td>{findingLabel('family', finding.family)}</td>
                  <td>
                    <StatusChip
                      state={finding.lifecycle_state}
                      overdueDays={finding.overdue_days}
                      dueAt={finding.due_at}
                      now={now}
                    />
                  </td>
                  <td>
                    {finding.due_at === null ? (
                      text('scr08.inbox.no_due')
                    ) : (
                      <FindingTime
                        instant={finding.due_at}
                        timeZone={timeZoneOf(plants, finding.plant_id)}
                      />
                    )}
                  </td>
                  <td className={styles.sticky}>
                    <Link
                      to={`/findings/${finding.finding_state_id}/loop`}
                      className={styles.rowAction}
                      data-access="loop"
                    >
                      {text('scr08.home.open_loop')}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {all.length > sorted.length && (
        <Link to="/findings/loops" data-part="overdue_more">
          {text('scr08.home.overdue_more')} ({all.length})
        </Link>
      )}
    </section>
  );
}

function RecentTable({
  summary,
  plants,
  permissions,
  now,
}: {
  readonly summary: HomeSummary;
  readonly plants: readonly Plant[];
  readonly permissions: ReadonlySet<string>;
  readonly now: number;
}) {
  return (
    <section aria-label={text('scr08.home.new_table')} className={styles.block}>
      <h2>{text('scr08.home.new_table')}</h2>
      {summary.recent.length === 0 ? (
        <p data-part="new_empty">{text('scr08.home.new_empty')}</p>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <Heading keys={['zone', 'family', 'state', 'occurred', 'next']} />
            <tbody>
              {summary.recent.map((finding) => (
                <tr key={finding.finding_state_id} data-finding={finding.finding_state_id}>
                  <th scope="row">
                    <Link to={`/findings/${finding.finding_state_id}`}>
                      {zoneName(plants, finding.zone_id)}
                    </Link>
                  </th>
                  <td>{findingLabel('family', finding.family)}</td>
                  <td>
                    <StatusChip state={finding.lifecycle_state} now={now} />
                  </td>
                  <td>
                    <FindingTime
                      instant={finding.occurred_at}
                      timeZone={timeZoneOf(plants, finding.plant_id)}
                    />
                  </td>
                  <td className={styles.sticky}>
                    <NextStepLink
                      findingId={finding.finding_state_id}
                      state={finding.lifecycle_state}
                      permissions={permissions}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Link to="/findings" data-part="open_inbox">
        {text('scr08.home.open_inbox_count')} (
        {summary.partial
          ? `${text('scr08.home.more_than')} ${String(summary.total)}`
          : summary.total}
        )
      </Link>
    </section>
  );
}

function useHomeReads(context: Context) {
  const [opening] = useState(() => (openings += 1));
  const [instant] = useState(() => new Date(findingsClock().now()).toISOString());
  const zones = context.plants.flatMap((plant) => plant.zones).slice(0, HOME_ZONES);
  const ids = zones.map((zone) => zone.zone_id);
  const canFindings = context.permissions.includes('findings.read');
  const canGates = context.permissions.includes('catalog.read');
  const canCoverage = context.permissions.includes('coverage.read');
  const ready = context.hierarchy.status === 'success';
  const key = (operation: ReadOperation, params: Parameters<typeof screenKey>[0]['params']) =>
    screenKey({
      screen: 'scr08',
      operation,
      scope: context.scope,
      concession: context.concession,
      ...(params === undefined ? {} : { params }),
    });
  const inbox = useScreenQuery({
    key: key(INBOX, { home: true, pages: HOME_PAGES }),
    policy: policyFor(INBOX),
    enabled: canFindings,
    read: ({ signal }) => readInbox({}, HOME_PAGES, signal),
  });
  const gates = useScreenQuery({
    key: key(GATES, { zones: ids }),
    policy: policyFor(GATES),
    enabled: canGates && ready,
    read: ({ signal }) => readGates(ids, signal),
  });
  const coverage = useScreenQuery({
    key: key(COVERAGE, { zones: ids, instant, opening }),
    policy: policyFor(COVERAGE),
    enabled: canCoverage && ready && ids.length > 0,
    read: ({ signal }) =>
      Promise.all(
        zones.map((zone) =>
          findingsPort.coverage.read(
            { path: { zone_id: zone.zone_id }, query: { instant } },
            signal,
          ),
        ),
      ),
  });
  return { inbox, gates, coverage, canFindings, canGates, canCoverage };
}

export default function Page() {
  const context = useFindingsContext();
  const { inbox, gates, coverage, canFindings, canGates, canCoverage } = useHomeReads(context);
  const permissions = new Set(context.permissions);
  const now = findingsClock().now();
  const gatesFor = canGates ? gates : null;
  // Sin compuertas leídas no se muestra ningún hallazgo: la compuerta manda (P9).
  const summary =
    inbox.data === undefined || gates.data === undefined
      ? null
      : homeSummary(inbox.data, gates.data, now);
  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>{text('scr08.home.title')}</h1>
        <PageRefresh
          screen="scr08"
          reads={[
            { fetchedAt: context.hierarchy.fetchedAt, operation: HIERARCHY },
            ...(canFindings ? [{ fetchedAt: inbox.fetchedAt, operation: INBOX }] : []),
            ...(canGates ? [{ fetchedAt: gates.fetchedAt, operation: GATES }] : []),
            ...(canCoverage ? [{ fetchedAt: coverage.fetchedAt, operation: COVERAGE }] : []),
          ]}
        />
      </header>
      {canFindings && (
        <section aria-label={text('scr08.home.urgent')} className={styles.block}>
          <h2>{text('scr08.home.urgent')}</h2>
          {!canGates ? (
            <ScreenState state="unavailable" />
          ) : (
            <Result query={inbox}>
              <Result query={gates}>
                {summary !== null && (
                  <>
                    {(inbox.data?.partitions.length ?? 0) > 0 && (
                      <p role="status">{text('scr08.inbox.partition_notices_hint')}</p>
                    )}
                    <UrgentCards
                      summary={summary}
                      canQueue={canTake('review_queue', permissions)}
                    />
                    {summary.partial && <p className={styles.hint}>{text('scr08.home.partial')}</p>}
                    <OverdueTable findings={summary.overdue} plants={context.plants} now={now} />
                  </>
                )}
              </Result>
            </Result>
          )}
        </section>
      )}
      <Result query={context.hierarchy}>
        <HomeZones
          plants={context.plants}
          gates={
            gatesFor ?? {
              data: [],
              status: 'success',
              error: null,
              refresh: () => Promise.resolve(),
            }
          }
          coverage={canCoverage ? coverage : null}
        />
      </Result>
      {canFindings && canGates && summary !== null && (
        <RecentTable
          summary={summary}
          plants={context.plants}
          permissions={permissions}
          now={now}
        />
      )}
    </main>
  );
}

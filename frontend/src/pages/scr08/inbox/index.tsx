/**
 * Bandeja de proyección: cursor opaco, filtros en bloque y refresco de la instantánea. Una sola
 * actualización, en la cabecera (VIG-365, S3-08): «Actualizar» repite todas las lecturas de la
 * página (la bandeja, sus compuertas y los vencidos).
 *
 * «Lazos de cierre» (VIG-365, S3-05) es esta misma bandeja con los estados de lazo vivo: abierto,
 * con acción, vencido o sin alternativa segura (`LOOP_STATES`), y el filtro de estado limitado a
 * ellos. No es una regla propia: son los estados del servidor con un registro del lazo pendiente.
 */
import { useState } from 'react';
import { DateInput } from '@/components/DateFields/DateFields';
import { useFormCheck } from '@/components/DateFields/useFormCheck';
import { ZoneHeader } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { PageRefresh } from '@/pages/shared/PageRefresh';
import { ScreenState } from '@/pages/shared/ScreenState';
import { readInbox, type InboxFilters } from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import {
  dayEnd,
  dayStart,
  filterTimeZone,
  OverdueHeader,
  Result,
  useFindingsContext,
} from '../shared';
import { FAMILIES, TIERS, STATES, findingLabel } from './FindingsView';
import { GatedFindingsView } from './GatedFindingsView';
import styles from './inbox.module.css';

/** Estados con el lazo vivo: lo que lista «Lazos de cierre». */
export const LOOP_STATES = ['open', 'action_assigned', 'overdue', 'unclosable'] as const;

export default function Page() {
  return <InboxScreen title="scr08.inbox.title" />;
}

export function InboxScreen({
  title,
  intro,
  states,
}: {
  readonly title: string;
  readonly intro?: string;
  /** Estados que la lista admite; sin ellos, todos. */
  readonly states?: readonly string[];
}) {
  const context = useFindingsContext();
  const check = useFormCheck();
  const [invalidPeriod, setInvalidPeriod] = useState(false);
  const [zoneId, setZoneId] = useState('');
  const timeZone = filterTimeZone(context.plants, zoneId === '' ? null : zoneId);
  const base: InboxFilters = states === undefined ? {} : { lifecycle_state: [...states] };
  const [selection, setSelection] = useState<{ filters: InboxFilters; pages: number }>({
    filters: base,
    pages: 1,
  });
  const offered = states === undefined ? STATES : STATES.filter((state) => states.includes(state));
  const operation = 'GET /loop/findings';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: { ...selection.filters, pages: selection.pages },
    }),
    policy: policyFor(operation),
    read: ({ signal }) => readInbox(selection.filters, selection.pages, signal),
  });
  return (
    <main>
      <header className={styles.header}>
        <h1>{text(title)}</h1>
        <PageRefresh
          screen="scr08"
          reads={[
            { fetchedAt: context.hierarchy.fetchedAt, operation: 'GET /hierarchy' },
            { fetchedAt: query.fetchedAt, operation },
          ]}
        />
      </header>
      {intro !== undefined && <p>{text(intro)}</p>}
      {states === undefined && <OverdueHeader context={context} />}
      <form
        className={styles.filters}
        aria-label={text('scr08.inbox.filters')}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (!check.run(event.currentTarget)) return;
          const data = new FormData(event.currentTarget);
          const family = FAMILIES.find((value) => value === data.get('family'));
          const tier = TIERS.find((value) => value === data.get('tier'));
          const state = offered.find((value) => value === data.get('state'));
          const zone = data.get('zone');
          const from = data.get('from');
          const to = data.get('to');
          // Los días se cuentan en la hora de la planta, no en UTC (S3-11).
          const start = typeof from === 'string' && from !== '' ? dayStart(from, timeZone) : null;
          const end = typeof to === 'string' && to !== '' ? dayEnd(to, timeZone) : null;
          if (
            (typeof from === 'string' && from !== '' && start === null) ||
            (typeof to === 'string' && to !== '' && end === null)
          ) {
            setInvalidPeriod(true);
            return;
          }
          setInvalidPeriod(false);
          setSelection({
            pages: 1,
            filters: {
              ...(family === undefined ? {} : { family }),
              ...(tier === undefined ? {} : { tier }),
              ...(state === undefined ? base : { lifecycle_state: [state] }),
              ...(typeof zone === 'string' && zone !== '' ? { zone_id: [zone] } : {}),
              ...(start === null ? {} : { from: start.toISOString() }),
              ...(end === null ? {} : { to: end.toISOString() }),
            },
          });
        }}
      >
        <label>
          {text('scr08.inbox.zone')}
          <select
            name="zone"
            value={zoneId}
            onChange={(event) => {
              setZoneId(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr08.inbox.all')}</option>
            {context.plants
              .flatMap((p) => p.zones)
              .map((z) => (
                <option key={z.zone_id} value={z.zone_id}>
                  {z.name}
                </option>
              ))}
          </select>
        </label>
        {(['family', 'tier', 'state'] as const).map((kind) => (
          <label key={kind}>
            {text(`scr08.inbox.${kind}`)}
            <select name={kind}>
              <option value="">{text('scr08.inbox.all')}</option>
              {(kind === 'family' ? FAMILIES : kind === 'tier' ? TIERS : offered).map((value) => (
                <option key={value} value={value}>
                  {findingLabel(kind, value)}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label>
          <span>
            {text('scr08.inbox.from')}
            <ZoneHeader timeZone={timeZone} />
          </span>
          <DateInput name="from" />
        </label>
        <label>
          <span>
            {text('scr08.inbox.to')}
            <ZoneHeader timeZone={timeZone} />
          </span>
          <DateInput name="to" />
        </label>
        <button type="submit">{text('scr08.inbox.apply')}</button>
      </form>
      {invalidPeriod && <p role="alert">{text('scr08.inbox.invalid_period')}</p>}
      <Result query={query}>
        {query.data !== undefined && <GatedFindingsView page={query.data} context={context} />}
        {query.data?.items.length === 0 && (
          <ScreenState state="empty" message={text('scr08.inbox.empty')} />
        )}
        {query.data?.next_cursor != null && (
          <button
            type="button"
            disabled={query.isFetching}
            onClick={() => {
              setSelection((current) => ({ ...current, pages: current.pages + 1 }));
            }}
          >
            {text('scr08.inbox.more')}
          </button>
        )}
      </Result>
    </main>
  );
}

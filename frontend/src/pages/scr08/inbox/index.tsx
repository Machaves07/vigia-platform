/** Bandeja de proyección: cursor opaco, filtros en bloque y refresco de la instantánea. */
import { useState } from 'react';
import { ScreenState } from '@/pages/shared/ScreenState';
import { readInbox, type InboxFilters } from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { FindingsNav, OverdueHeader, Refresh, Result, useFindingsContext } from '../shared';
import { FAMILIES, TIERS, STATES, findingLabel } from './FindingsView';
import { GatedFindingsView } from './GatedFindingsView';
import styles from './inbox.module.css';

export default function Page() {
  const context = useFindingsContext();
  const [invalidPeriod, setInvalidPeriod] = useState(false);
  const [selection, setSelection] = useState<{ filters: InboxFilters; pages: number }>({
    filters: {},
    pages: 1,
  });
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
      <FindingsNav />
      <h1>{text('scr08.inbox.title')}</h1>
      <OverdueHeader context={context} />
      <form
        className={styles.filters}
        aria-label={text('scr08.inbox.filters')}
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const family = FAMILIES.find((value) => value === data.get('family'));
          const tier = TIERS.find((value) => value === data.get('tier'));
          const state = STATES.find((value) => value === data.get('state'));
          const zone = data.get('zone');
          const from = data.get('from');
          const to = data.get('to');
          const end =
            typeof to === 'string' && to !== ''
              ? new Date(Date.parse(`${to}T00:00:00.000Z`) + 86_400_000)
              : null;
          if (end !== null && !Number.isFinite(end.getTime())) {
            setInvalidPeriod(true);
            return;
          }
          setInvalidPeriod(false);
          setSelection({
            pages: 1,
            filters: {
              ...(family === undefined ? {} : { family }),
              ...(tier === undefined ? {} : { tier }),
              ...(state === undefined ? {} : { lifecycle_state: [state] }),
              ...(typeof zone === 'string' && zone !== '' ? { zone_id: [zone] } : {}),
              ...(typeof from === 'string' && from !== '' ? { from: `${from}T00:00:00.000Z` } : {}),
              ...(typeof to === 'string' && to !== '' ? { to: end?.toISOString() ?? null } : {}),
            },
          });
        }}
      >
        <label>
          {text('scr08.inbox.zone')}
          <select name="zone">
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
              {(kind === 'family' ? FAMILIES : kind === 'tier' ? TIERS : STATES).map((value) => (
                <option key={value} value={value}>
                  {findingLabel(kind, value)}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label>
          {text('scr08.inbox.from')}
          <input type="date" name="from" />
        </label>
        <label>
          {text('scr08.inbox.to')}
          <input type="date" name="to" />
        </label>
        <button type="submit">{text('scr08.inbox.apply')}</button>
      </form>
      {invalidPeriod && <p role="alert">{text('scr08.inbox.invalid_period')}</p>}
      <Refresh query={query} operation={operation} />
      <p>{text('scr08.inbox.projection')}</p>
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

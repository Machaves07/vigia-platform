/** Puerta del recorrido diario: secciones autorizadas, nunca contadores de personas. */
import { Link } from 'react-router';
import { findingsPort, readInbox } from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import {
  FindingsNav,
  FindingTime,
  OverdueHeader,
  Refresh,
  Result,
  useFindingsContext,
} from '../shared';
import { GatedFindingsView } from '../inbox/GatedFindingsView';
import { HomeZones } from './HomeZones';

export default function Page() {
  const context = useFindingsContext();
  return (
    <main>
      <FindingsNav />
      <h1>{text('scr08.home.title')}</h1>
      {context.permissions.includes('coverage.read') && <HomeZones context={context} />}
      {context.permissions.includes('findings.read') && (
        <>
          <HomeFindings context={context} kind="new" />
          <HomeFindings context={context} kind="due" />
          <Queue context={context} />
        </>
      )}
      <OverdueHeader context={context} />
    </main>
  );
}

function HomeFindings({
  context,
  kind,
}: {
  readonly context: ReturnType<typeof useFindingsContext>;
  readonly kind: 'new' | 'due';
}) {
  const filters = {
    lifecycle_state: kind === 'new' ? ['received', 'in_review'] : ['action_assigned'],
  };
  const operation = 'GET /loop/findings';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: filters,
    }),
    policy: policyFor(operation),
    read: ({ signal }) => readInbox(filters, 1, signal),
  });
  return (
    <section aria-label={text(`scr08.home.${kind}`)}>
      <h2>{text(`scr08.home.${kind}`)}</h2>
      <Refresh query={query} operation={operation} />
      <Result query={query} empty={query.data?.items.length === 0}>
        {query.data !== undefined && <GatedFindingsView page={query.data} context={context} />}
      </Result>
      <Link to="/findings">{text('scr08.home.open_inbox')}</Link>
    </section>
  );
}

function Queue({ context }: { readonly context: ReturnType<typeof useFindingsContext> }) {
  const operation = 'GET /loop/review-queue';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation,
      scope: context.scope,
      concession: context.concession,
    }),
    policy: policyFor(operation),
    read: ({ signal }) => findingsPort.queue.read({ query: { limit: 50 } }, signal),
  });
  return (
    <section aria-label={text('scr08.home.queue')}>
      <h2>{text('scr08.home.queue')}</h2>
      <Refresh query={query} operation={operation} />
      <Result query={query} empty={query.data?.items.length === 0}>
        <ul>
          {query.data?.items.map((item) => {
            const plant = context.plants.find((p) => p.plant_id === item.plant_id);
            return (
              <li key={item.detection_id}>
                {plant?.zones.find((z) => z.zone_id === item.zone_id)?.name ?? item.zone_id}
                {' · '}
                <FindingTime instant={item.review_due_at} timeZone={plant?.timezone ?? ''} />
              </li>
            );
          })}
        </ul>
      </Result>
      <Link to="/review-queue">{text('scr08.home.open_queue')}</Link>
    </section>
  );
}

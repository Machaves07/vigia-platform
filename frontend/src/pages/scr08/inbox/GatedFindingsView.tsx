/** La compuerta sustituye la fila completa; no convierte una zona no productiva en cero. */
import { ScreenState } from '@/pages/shared/ScreenState';
import { readGates, type InboxPage } from '@/ports/scr08Findings';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { Refresh, Result, useFindingsContext } from '../shared';
import { FindingsView } from './FindingsView';

export function GatedFindingsView({
  page,
  context,
}: {
  readonly page: InboxPage;
  readonly context: ReturnType<typeof useFindingsContext>;
}) {
  const ids = [...new Set(page.items.map((item) => item.zone_id))].sort();
  const allowed = context.permissions.includes('catalog.read');
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr08',
      operation: 'GET /zones/{zone_id}/gates',
      scope: context.scope,
      concession: context.concession,
      params: { zones: ids },
    }),
    policy: policyFor('GET /zones/{zone_id}/gates'),
    enabled: allowed && ids.length > 0,
    read: ({ signal }) => readGates(ids, signal),
  });
  const permissions = new Set(context.permissions);
  if (ids.length === 0)
    return (
      <FindingsView page={page} plants={context.plants} gates={[]} permissions={permissions} />
    );
  if (!allowed) return <ScreenState state="unavailable" />;
  return (
    <>
      <Refresh query={query} operation="GET /zones/{zone_id}/gates" />
      <Result query={query}>
        <FindingsView
          page={page}
          plants={context.plants}
          gates={query.data ?? []}
          permissions={permissions}
        />
      </Result>
    </>
  );
}

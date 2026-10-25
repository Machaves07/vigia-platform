/** A-54: dos lecturas individuales; la diferencia se calcula localmente sobre campos permitidos. */
import { useCallback, useId, useState } from 'react';
import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import type { KeysetRequest } from '@/components/KeysetTable/keyset';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  catalogClock,
  catalogPayload,
  catalogPort,
  readVersion,
  type CatalogSummary,
} from '@/ports/scr04Catalog';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { useUiSession } from '@/state/session';
import { Refresh, Result, useCatalogQuery } from './shared';
import { ComparisonView } from './ComparisonView';

function Comparison({
  zoneId,
  pair,
  concessionOrganizationId,
}: {
  readonly zoneId: string;
  readonly pair: readonly [number, number];
  readonly concessionOrganizationId: string | null;
}) {
  const { principal } = useUiSession();
  const operation = 'GET /zones/{zone_id}/catalog/versions/{catalog_version}';
  const query = useCatalogQuery(
    operation,
    `${zoneId}:${String(pair[0])}:${String(pair[1])}`,
    async (signal) => {
      const versions = await Promise.all(
        pair.map((catalog_version) =>
          readVersion(
            zoneId,
            catalog_version,
            principal?.organization.organization_id ?? '',
            signal,
            concessionOrganizationId,
          ),
        ),
      );
      const before = versions[0];
      const after = versions[1];
      if (before === undefined || after === undefined) throw new Error('Faltan versiones.');
      return {
        before: { ...before, catalog: catalogPayload(before) },
        after: { ...after, catalog: catalogPayload(after) },
      };
    },
  );
  return (
    <>
      <Refresh query={query} />
      <Result query={query}>
        {query.data === undefined ? null : (
          <ComparisonView
            {...query.data}
            organizationId={principal?.organization.organization_id ?? ''}
            concessionOrganizationId={concessionOrganizationId}
          />
        )}
      </Result>
    </>
  );
}

export function Versions({
  zoneId,
  timeZone,
  concessionOrganizationId = null,
}: {
  readonly zoneId: string;
  readonly timeZone: string;
  readonly concessionOrganizationId?: string | null;
}) {
  const id = useId();
  const [pair, setPair] = useState<readonly [number, number] | null>(null);
  const fetchPage = useCallback(
    async (request: KeysetRequest, signal: AbortSignal) => {
      const page = await catalogPort.history.read(
        {
          path: { zone_id: zoneId },
          query: { after: request.cursor?.cursor ?? null, limit: request.pageSize },
        },
        signal,
      );
      return {
        items: page.versions,
        nextCursor: page.next_after === null ? null : { cursor: page.next_after },
      };
    },
    [zoneId],
  );
  return (
    <section aria-label={text('scr04.catalog.history')}>
      <h2>{text('scr04.catalog.history')}</h2>
      <KeysetTable<CatalogSummary>
        caption={text('scr04.catalog.history')}
        clock={catalogClock()}
        fetchPage={fetchPage}
        rowKey={(row) => String(row.catalog_version)}
        columns={[
          {
            id: 'version',
            header: text('scr04.catalog.version'),
            cell: (row) => row.catalog_version,
          },
          {
            id: 'issued_at',
            header: text('scr04.catalog.effective_from'),
            cell: (row) => <TimestampWithOffset instant={row.issued_at} timeZone={timeZone} />,
          },
          { id: 'issued_by', header: text('scr04.catalog.author'), cell: (row) => row.issued_by },
          { id: 'reason_es', header: text('scr04.catalog.reason'), cell: (row) => row.reason_es },
          {
            id: 'changed_fields',
            header: text('scr04.catalog.changed'),
            cell: (row) =>
              row.changed_fields.map((field) => label('catalog_changed_field', field)).join(', '),
          },
        ]}
      />
      <form
        aria-label={text('scr04.catalog.compare')}
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const from = Number(data.get('from'));
          const to = Number(data.get('to'));
          if (
            Number.isSafeInteger(from) &&
            Number.isSafeInteger(to) &&
            from > 0 &&
            to > 0 &&
            from !== to
          )
            setPair([from, to]);
        }}
      >
        <label htmlFor={`${id}-from`}>{text('scr04.catalog.from')}</label>
        <input id={`${id}-from`} name="from" type="number" min="1" step="1" required />
        <label htmlFor={`${id}-to`}>{text('scr04.catalog.to')}</label>
        <input id={`${id}-to`} name="to" type="number" min="1" step="1" required />
        <button type="submit">{text('scr04.catalog.compare')}</button>
      </form>
      {pair === null ? null : (
        <Comparison
          key={pair.join(':')}
          zoneId={zoneId}
          pair={pair}
          concessionOrganizationId={concessionOrganizationId}
        />
      )}
    </section>
  );
}

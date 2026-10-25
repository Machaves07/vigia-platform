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
  type CatalogDetail,
  type CatalogSummary,
} from '@/ports/scr04Catalog';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { useUiSession } from '@/state/session';
import { Refresh, Result, useCatalogQuery } from './shared';

function fields(version: CatalogDetail): Record<string, string> {
  const catalog = catalogPayload(version);
  const values: Record<string, string> = {
    [text('scr04.catalog.single_occupancy')]: text(
      version.single_occupancy ? 'scr04.catalog.yes' : 'scr04.catalog.no',
    ),
    [text('scr04.catalog.window')]: String(version.aggregation_window_minutes),
    [text('scr04.catalog.review')]: String(catalog.thresholds.review),
    [text('scr04.catalog.publication')]: String(catalog.thresholds.publication),
    [text('scr04.catalog.coverage')]: JSON.stringify({
      required_count: catalog.minimum_coverage.required_count,
      required_camera_ids: catalog.minimum_coverage.required_camera_ids,
    }),
    [text('scr04.catalog.clip_window')]:
      `${String(catalog.clip_window.pre_seconds)} / ${String(catalog.clip_window.post_seconds)}`,
    [text('scr04.catalog.episode')]: JSON.stringify({
      grouping_window_ms: catalog.episode.grouping_window_ms,
      max_segment_ms: catalog.episode.max_segment_ms,
    }),
    [text('scr04.catalog.cameras')]: JSON.stringify(
      catalog.cameras.map((camera) => [
        camera.camera_id,
        camera.code,
        label('camera_role_in_zone', camera.role_in_zone),
        camera.declared_min_fps,
      ]),
    ),
    [text('scr04.catalog.signals')]: JSON.stringify(
      catalog.signals.map((signal) => [
        signal.signal_id,
        signal.code,
        label('signal_role', signal.role),
        label('signal_level', signal.asserted_level),
        signal.source.reader,
        signal.source.channel,
        signal.description_es,
      ]),
    ),
  };
  for (const standard of catalog.standards) {
    const prefix = `${text('scr04.catalog.standard')} ${standard.standard_id}`;
    values[`${prefix} · ${text('scr04.catalog.title_es')}`] = standard.title_es;
    values[`${prefix} · ${text('scr04.catalog.declared_text')}`] = standard.declared_text;
    values[`${prefix} · ${text('scr04.catalog.family')}`] = label(
      'predicate_family',
      standard.family,
    );
    values[`${prefix} · ${text('scr04.catalog.tier')}`] = text('scr04.catalog.tier_policy_signal');
    values[`${prefix} · ${text('scr04.catalog.author')}`] = standard.declared_by.display_name;
    values[`${prefix} · ${text('scr04.catalog.effective_from')}`] = standard.effective_from;
    values[`${prefix} · ${text('scr04.catalog.predicate')}`] = JSON.stringify(
      standard.predicate.all_of
        .map((condition) =>
          'presence' in condition
            ? text('scr04.catalog.presence')
            : `${label('signal_role', condition.signal_role)}: ${label('signal_value', condition.value)}`,
        )
        .sort(),
    );
    values[`${prefix} · ${text('scr04.catalog.duration')}`] = String(
      standard.predicate.min_duration_ms,
    );
  }
  return values;
}

function compareVersions(before: CatalogDetail, after: CatalogDetail) {
  const left = fields(before);
  const right = fields(after);
  return [...new Set([...Object.keys(left), ...Object.keys(right)])]
    .sort()
    .filter((field) => left[field] !== right[field])
    .map((field) => ({ field, before: left[field] ?? '—', after: right[field] ?? '—' }));
}

function Comparison({
  zoneId,
  pair,
  timeZone,
}: {
  readonly zoneId: string;
  readonly pair: readonly [number, number];
  readonly timeZone: string;
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
          ),
        ),
      );
      const before = versions[0];
      const after = versions[1];
      if (before === undefined || after === undefined) throw new Error('Faltan versiones.');
      return { before, after, changes: compareVersions(before, after) };
    },
  );
  return (
    <>
      <Refresh query={query} />
      <Result query={query}>
        <h3>{text('scr04.catalog.changed')}</h3>
        {query.data?.changes.length === 0 ? <p>{text('scr04.catalog.no_changes')}</p> : null}
        <table>
          <caption>{text('scr04.catalog.compare')}</caption>
          <thead>
            <tr>
              <th scope="col">{text('scr04.catalog.changed')}</th>
              <th scope="col">{text('scr04.catalog.before')}</th>
              <th scope="col">{text('scr04.catalog.after')}</th>
            </tr>
          </thead>
          <tbody>
            {query.data?.changes.map((change) => (
              <tr key={change.field}>
                <th scope="row">{change.field}</th>
                <td>{change.before}</td>
                <td>{change.after}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {[query.data?.before, query.data?.after].map((version) =>
          version === undefined ? null : (
            <details key={version.catalog_version}>
              <summary>
                {text('scr04.catalog.signed')} {version.catalog_version}
              </summary>
              <p>
                <TimestampWithOffset instant={version.issued_at} timeZone={timeZone} /> ·{' '}
                {version.issued_by}
              </p>
              <p>{version.reason_es}</p>
              <dl>
                {['key_id', 'signature', 'payload_canonical_sha256', 'signed_at'].map((key) => (
                  <div key={key}>
                    <dt>{text(`scr04.catalog.${key}`)}</dt>
                    <dd>
                      {typeof version.envelope[key] === 'string'
                        ? version.envelope[key]
                        : text('common.unknown')}
                    </dd>
                  </div>
                ))}
              </dl>
            </details>
          ),
        )}
      </Result>
    </>
  );
}

export function Versions({
  zoneId,
  timeZone,
}: {
  readonly zoneId: string;
  readonly timeZone: string;
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
        <Comparison key={pair.join(':')} zoneId={zoneId} pair={pair} timeZone={timeZone} />
      )}
    </section>
  );
}

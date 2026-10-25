/** Estado auditado a demanda, con pruebas de integridad junto a las descargas. */
import { useState } from 'react';
import {
  exportsPort,
  exportReadView,
  manifestHash,
  type ExportJob,
  type ExportState,
  type LedgerPage,
  type Plant,
} from '@/ports/scr14Exports';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { Downloads } from './Downloads';
import { CheckpointsView, ExportView } from './ExportView';
import { Refresh, Result } from './shared';

export function Job({
  exportId,
  initial,
  scope,
  concession,
  plants,
}: {
  readonly exportId: string;
  readonly initial: ExportJob | null;
  readonly scope: string;
  readonly concession: string | null;
  readonly plants: readonly Plant[];
}) {
  const [conflict, setConflict] = useState<ExportState | null>(null);
  const status = useScreenQuery({
    key: screenKey({
      screen: 'scr14',
      operation: 'GET /accreditation/exports/{export_id}',
      scope,
      concession,
      params: { export_id: exportId },
    }),
    policy: policyFor('GET /accreditation/exports/{export_id}'),
    // Montado únicamente tras solicitar o consultar explícitamente un identificador.
    enabled: initial === null,
    read: ({ signal }) => exportsPort.status.read({ path: { export_id: exportId } }, signal),
  });
  const job = status.data ?? initial;
  const timeZone = plants.find((plant) => plant.plant_id === job?.plant_id)?.timezone ?? 'UTC';
  const verifier = useScreenQuery({
    key: screenKey({
      screen: 'scr14',
      operation: 'GET /.well-known/vigia-verifier',
      scope,
      concession,
    }),
    policy: policyFor('GET /.well-known/vigia-verifier'),
    enabled: false,
    read: ({ signal }) => exportsPort.verifier.read({}, signal),
  });
  const checkpoints = useScreenQuery({
    key: screenKey({ screen: 'scr14', operation: 'GET /integrity/checkpoints', scope, concession }),
    policy: policyFor('GET /integrity/checkpoints'),
    read: ({ signal }) => exportsPort.checkpoints.read({}, signal),
  });
  const proof = useScreenQuery({
    key: screenKey({
      screen: 'scr14',
      operation: 'GET /ledger/records',
      scope,
      concession,
      params: { export_id: exportId, plant_id: job?.plant_id ?? '' },
    }),
    policy: policyFor('GET /ledger/records'),
    read: async ({ signal }): Promise<string | null> => {
      if (job === null) return null;
      let cursor: LedgerPage['next_cursor'] = null;
      const seen = new Set<string>();
      do {
        const page: LedgerPage = await exportsPort.records.read(
          {
            query: {
              record_type: ['export_produced'],
              plant_id: job.plant_id,
              received_from: job.requested_at,
              ...(cursor === null
                ? {}
                : { after_received_at: cursor.received_at, after_record_id: cursor.record_id }),
            },
          },
          signal,
        );
        const hash = manifestHash(page, job, scope);
        if (hash !== null) return hash;
        cursor = page.next_cursor;
        if (cursor !== null) {
          const key = `${cursor.received_at}:${cursor.record_id}`;
          if (seen.has(key)) throw new Error('Cursor repetido en la prueba del manifiesto.');
          seen.add(key);
        }
      } while (cursor !== null);
      return null;
    },
  });
  return (
    <section aria-label={exportId}>
      <h2>{text('scr14.status')}</h2>
      <code>{exportId}</code>
      <Refresh
        query={{
          ...status,
          fetchedAt:
            status.fetchedAt ?? (initial === null ? null : Date.parse(initial.requested_at)),
          refresh: async () => {
            setConflict(null);
            await status.refresh();
          },
        }}
      />
      <Result query={{ ...status, status: job === null ? status.status : 'success' }}>
        {job !== null && (
          <ExportView
            job={exportReadView(conflict === null ? job : { ...job, export_state: conflict })}
            organizationId={scope}
            timeZone={timeZone}
          />
        )}
      </Result>
      <Downloads exportId={exportId} timeZone={timeZone} onConflict={setConflict} />
      <section aria-label={text('scr14.hash')}>
        <h3>{text('scr14.hash')}</h3>
        <button
          type="button"
          disabled={job === null || proof.isFetching}
          onClick={() => {
            void proof.refresh();
          }}
        >
          {text('scr14.manifest_hash')}
        </button>
        <Result query={proof}>
          {proof.data === undefined ? null : proof.data === null ? (
            <p>{text('scr14.hash_missing')}</p>
          ) : (
            <code>{proof.data}</code>
          )}
        </Result>
      </section>
      <section aria-label={text('scr14.verifier')}>
        <h3>{text('scr14.verifier')}</h3>
        <Refresh query={verifier} />
        <Result query={verifier}>
          {verifier.data === undefined ? null : <code>{verifier.data.sha256}</code>}
        </Result>
      </section>
      <section aria-label={text('scr14.checkpoints')}>
        <h3>{text('scr14.checkpoints')}</h3>
        <Refresh query={checkpoints} />
        <Result query={checkpoints}>
          {checkpoints.data === undefined ? null : (
            <CheckpointsView data={checkpoints.data} organizationId={scope} />
          )}
        </Result>
      </section>
    </section>
  );
}

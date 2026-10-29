/** UI-14 se monta solo tras un clic. La API entrega una sola página, sin cursor ni total. */
import { useState } from 'react';
import { usePlantTimeZone } from '@/app/AppShell';
import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  integrityClock,
  integrityFailure,
  integrityPort,
  type IntegrityResult,
  type Plant,
} from '@/ports/scr03Integrity';
import { policyFor } from '@/state/query';
import { text } from '@/texts';
import { resultColumns } from './IntegrityViews';

export function ResultsTable({ plants }: { readonly plants: readonly Plant[] }) {
  const timeZone = usePlantTimeZone();
  const [error, setError] = useState<Error | null>(null);
  return (
    <>
      {error === null ? null : <ScreenState state={integrityFailure(error)} />}
      <KeysetTable<IntegrityResult>
        caption={text('scr03.latest_results')}
        columns={resultColumns(plants, timeZone)}
        rowKey={(row) => `${row.chain.kind}:${row.chain.plant_id ?? 'organization'}`}
        clock={integrityClock()}
        refreshPolicy={
          policyFor('GET /integrity/results').explicitOnly ? 'on_demand' : 'periodic_60s'
        }
        emptyText={text('scr03.empty')}
        fetchPage={async (_request, signal) => {
          setError(null);
          try {
            const data = await integrityPort.results.read({}, signal);
            return { items: data.results, nextCursor: null };
          } catch (failure) {
            if (!signal.aborted)
              setError(
                failure instanceof Error ? failure : new Error('Falló la consulta de integridad.'),
              );
            throw failure;
          }
        }}
      />
    </>
  );
}

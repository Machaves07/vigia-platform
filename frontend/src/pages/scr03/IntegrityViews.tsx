/** RV-05: proyección explícita, sin propagar campos adicionales de las respuestas al DOM. */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  belongsToOrganization,
  verifierDownload,
  type CheckpointKeys,
  type Checkpoints,
  type IntegrityResult,
  type Plant,
} from '@/ports/scr03Integrity';
import { text } from '@/texts';

export function VerifierView({ sha256 }: { readonly sha256: string }) {
  return (
    <>
      <p>
        {text('scr03.published_hash')}: <code>{sha256}</code>
      </p>
      <a href={verifierDownload.href} download={verifierDownload.name}>
        {text('scr03.download')}
      </a>
    </>
  );
}

export function PublicKeyView({ keyData }: { readonly keyData: CheckpointKeys['keys'][number] }) {
  return (
    <>
      <code>{keyData.key_id}</code>
      <pre>{keyData.public_key}</pre>
    </>
  );
}

function chainName(chain: IntegrityResult['chain'], plants: readonly Plant[]): string {
  if (chain.kind === 'audit') return text('scr03.organization_audit');
  if (chain.plant_id === null) return text('scr03.organization_ledger');
  return `${text('scr03.plant_ledger')}: ${plants.find((plant) => plant.plant_id === chain.plant_id)?.name ?? text('scr03.unknown')}`;
}

function ResultCell({ result }: { readonly result: IntegrityResult }) {
  return (
    <>
      {result.result === 'intact'
        ? text('scr03.intact')
        : result.result === 'broken' && result.broken_sequence !== null
          ? `${text('scr03.broken')} ${String(result.broken_sequence)}`
          : text('scr03.unknown')}
    </>
  );
}

export function ResultDate({
  instant,
  timeZone,
}: {
  readonly instant: string | null;
  readonly timeZone: string;
}) {
  return instant === null ? (
    <>{text('scr03.no_date')}</>
  ) : (
    <TimestampWithOffset instant={instant} timeZone={timeZone} />
  );
}

export function ResultsView({
  results,
  plants,
  timeZone,
}: {
  readonly results: readonly IntegrityResult[];
  readonly plants: readonly Plant[];
  readonly timeZone: string;
}) {
  if (results.length === 0) return <ScreenState state="empty" message={text('scr03.empty')} />;
  const columns = resultColumns(plants, timeZone);
  return (
    <table>
      <caption>{text('scr03.history')}</caption>
      <thead>
        <tr>
          {columns.map((column) => (
            <th scope="col" key={column.id}>
              {column.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {results.map((result, index) => (
          <tr key={index}>
            {columns.map((column) => (
              <td key={column.id}>{column.cell(result)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Las mismas celdas se usan en UI-14 y en el registro de RV-05. */
export function resultColumns(
  plants: readonly Plant[],
  timeZone: string,
): readonly KeysetColumn<IntegrityResult>[] {
  return [
    { id: 'chain', header: text('scr03.chain'), cell: (row) => chainName(row.chain, plants) },
    { id: 'sequence', header: text('scr03.sequence'), cell: (row) => row.head_sequence },
    { id: 'result', header: text('scr03.result'), cell: (row) => <ResultCell result={row} /> },
    {
      id: 'verified_at',
      header: text('scr03.verified_at'),
      cell: (row) => (
        <ResultDate
          instant={row.verified_at}
          timeZone={
            plants.find((plant) => plant.plant_id === row.chain.plant_id)?.timezone ?? timeZone
          }
        />
      ),
    },
  ];
}

export function CheckpointsView({
  data,
  keys,
  plants,
  timeZone,
  organizationId,
  concessionOrganizationId = null,
  reportContractError,
}: {
  readonly data: Checkpoints;
  readonly keys: CheckpointKeys;
  readonly plants: readonly Plant[];
  readonly timeZone: string;
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { code: string }) => void;
}) {
  if (
    !belongsToOrganization(data.organization_id, {
      organizationId,
      concessionOrganizationId,
      ...(reportContractError === undefined ? {} : { reportContractError }),
    })
  )
    return <ScreenState state="error" />;
  if (data.checkpoints.length === 0)
    return <ScreenState state="empty" message={text('scr03.empty')} />;
  return (
    <table>
      <caption>{text('scr03.checkpoints')}</caption>
      <thead>
        <tr>
          {['chain', 'checkpoint_sequence', 'taken_at', 'key'].map((key) => (
            <th scope="col" key={key}>
              {text(`scr03.${key}`)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.checkpoints.map((point, index) => {
          const publicKey = keys.keys.find((key) => key.key_id === point.key_id);
          return (
            <tr key={index}>
              <td>{chainName(point.chain, plants)}</td>
              <td>{point.sequence}</td>
              <td>
                <ResultDate
                  instant={point.taken_at}
                  timeZone={
                    plants.find((plant) => plant.plant_id === point.chain.plant_id)?.timezone ??
                    timeZone
                  }
                />
              </td>
              <td>
                {publicKey === undefined ? (
                  <>
                    <code>{point.key_id}</code>
                    <p>{text('scr03.key_missing')}</p>
                  </>
                ) : (
                  <PublicKeyView keyData={publicKey} />
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

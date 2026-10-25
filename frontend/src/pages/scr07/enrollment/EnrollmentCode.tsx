/**
 * Código de alta (IF-14 paso 3; BR-APP-50; pendiente nº 30): `POST /nodes/{node_id}/enrollment-codes`
 * muestra **una sola vez** el código y su vencimiento en `CopyOnceSecret`, junto al `node_id` y a
 * **todas** las huellas `node_ca_root_sha256[]`, con la instrucción de compararlas con las que
 * imprime `enroll`. Se puede pedir otro mientras el servidor lo admita (`declared` o re-alta); si
 * no, responde `fleet_node_not_declared`.
 *
 * El código nunca entra en la caché: la escritura lo entrega al estado de esta página y la
 * mutación solo guarda `code_id`. Al salir o al confirmar «lo guardé», desaparece.
 */
import { useId, useState } from 'react';
import { CopyOnceSecret } from '@/components/CopyOnceSecret/CopyOnceSecret';
import {
  fleetNotice,
  fleetPort,
  type EnrollmentAttempt,
  type EnrollmentCode as Issued,
} from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { FleetTime } from '../inventory/FleetView';
import { FleetNav, FleetNotice, FleetRefresh, FleetResult, useFleetContext } from '../shared';

/** Intentos de alta: momento, resultado etiquetado, huella del equipo y versiones. */
export function AttemptsTable({
  attempts,
  timeZone,
}: {
  readonly attempts: readonly EnrollmentAttempt[];
  readonly timeZone: string;
}) {
  if (attempts.length === 0) return <p>{text('scr07.attempts.none')}</p>;
  return (
    <table>
      <caption>{text('scr07.attempts.title')}</caption>
      <thead>
        <tr>
          <th scope="col">{text('scr07.attempts.at')}</th>
          <th scope="col">{text('scr07.attempts.result')}</th>
          <th scope="col">{text('scr07.attempts.hardware')}</th>
          <th scope="col">{text('scr07.attempts.versions')}</th>
        </tr>
      </thead>
      <tbody>
        {attempts.map((attempt) => (
          <tr key={attempt.attempt_id}>
            <td>
              <FleetTime instant={attempt.attempted_at} timeZone={timeZone} />
            </td>
            <td>{label('enrollment_attempt_result', attempt.result)}</td>
            <td>
              <code>{attempt.hardware_fingerprint}</code>
            </td>
            <td>{`${attempt.software_version} / ${attempt.contract_version}`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Attempts({ nodeId, timeZone }: { readonly nodeId: string; readonly timeZone: string }) {
  const context = useFleetContext();
  const [after, setAfter] = useState<string | null>(null);
  const operation = 'GET /nodes/{node_id}/enrollment-attempts';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: { node_id: nodeId, after },
    }),
    policy: policyFor(operation),
    read: ({ signal }) =>
      fleetPort.attempts.read({ path: { node_id: nodeId }, query: { after, limit: 50 } }, signal),
  });
  const attempts = query.data?.attempts ?? [];
  return (
    <section aria-label={text('scr07.attempts.title')}>
      <h2>{text('scr07.attempts.title')}</h2>
      <FleetRefresh query={query} operation={operation} />
      <FleetResult query={query} empty={false}>
        <AttemptsTable attempts={attempts} timeZone={timeZone} />
        {query.data?.next_after != null && (
          <button
            type="button"
            onClick={() => {
              setAfter(query.data?.next_after ?? null);
            }}
          >
            {text('scr07.load_more')}
          </button>
        )}
      </FleetResult>
    </section>
  );
}

export function EnrollmentCode({ nodeId }: { readonly nodeId: string }) {
  const id = useId();
  const context = useFleetContext();
  const [issued, setIssued] = useState<Issued | null>(null);
  const mutation = useScreenMutation({
    write: async () => {
      const value = await fleetPort.issueCode.write({ path: { node_id: nodeId } });
      setIssued(value);
      return { code_id: value.code_id };
    },
    invalidates: { screen: 'scr07', resources: ['nodes'] },
  });
  const notice = fleetNotice(mutation.error);
  const node = context.nodes.find((item) => item.node_id === nodeId);
  const timeZone =
    context.plants.find((plant) => plant.plant_id === node?.plant_id)?.timezone ?? '';
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.enrollment.title')}</h1>
      <p>
        {text('components_forms.secret.node_id')}: <code>{nodeId}</code>
        {node === undefined ? null : ` · ${node.code}`}
      </p>
      <p>{text('scr07.enrollment.guide')}</p>
      <FleetNotice notice={notice} field="form" id={`${id}-form`} />
      <button
        type="button"
        disabled={mutation.status === 'pending'}
        onClick={() => {
          setIssued(null);
          mutation.reset();
          void mutation.run(undefined).catch(() => undefined);
        }}
      >
        {text('scr07.enrollment.issue')}
      </button>
      {issued !== null && (
        <section aria-label={text('scr07.enrollment.issued')}>
          <h2>{text('scr07.enrollment.issued')}</h2>
          <p>{text('scr07.enrollment.compare')}</p>
          <CopyOnceSecret
            key={issued.code_id}
            kind="enrollment_code"
            value={issued.code}
            nodeId={nodeId}
            fingerprints={issued.node_ca_root_sha256}
            onAcknowledged={() => {
              setIssued(null);
            }}
          />
          <p>
            {text('scr07.enrollment.expires')}{' '}
            <FleetTime instant={issued.expires_at} timeZone={timeZone} />
          </p>
        </section>
      )}
      <Attempts nodeId={nodeId} timeZone={timeZone} />
    </main>
  );
}

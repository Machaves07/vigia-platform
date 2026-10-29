/** IF-18: verificar es una escritura asíncrona, nunca un disparador de sondeos. */
import { useState } from 'react';
import { Link } from 'react-router';
import {
  integrityPort,
  type VerificationAccepted,
  type VerifyRequest,
} from '@/ports/scr03Integrity';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { usePlantTimeZone } from '@/app/AppShell';
import { text } from '@/texts';
import { CheckpointsView, ResultDate, VerifierView } from '../IntegrityViews';
import { useUiSession } from '@/state/session';
import { ResultsTable } from '../ResultsTable';
import { IntegrityQueryState, IntegrityRefresh, useIntegrityContext } from '../shared';
import styles from '../scr03.module.css';

function Integrity() {
  const context = useIntegrityContext();
  // VIG-366: la hora en la zona de la planta del alcance, no en UTC.
  const scopeZone = usePlantTimeZone();
  const [chain, setChain] = useState('ledger');
  const [accepted, setAccepted] = useState<VerificationAccepted | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const key = (
    operation:
      'GET /integrity/results' | 'GET /integrity/checkpoints' | 'GET /.well-known/vigia-verifier',
  ) =>
    screenKey({
      screen: 'scr03',
      operation,
      scope: context.organizationId,
      concession: context.concession,
    });
  const checkpoints = useScreenQuery({
    key: key('GET /integrity/checkpoints'),
    policy: policyFor('GET /integrity/checkpoints'),
    read: async ({ signal }) => {
      const data = await integrityPort.checkpoints.read({}, signal);
      const keys = await integrityPort.keys.read({}, signal);
      return { data, keys };
    },
  });
  const verifier = useScreenQuery({
    key: key('GET /.well-known/vigia-verifier'),
    policy: policyFor('GET /.well-known/vigia-verifier'),
    enabled: false,
    read: ({ signal }) => integrityPort.verifier.read({}, signal),
  });
  const verification = useScreenMutation({
    write: (body: VerifyRequest) => integrityPort.verify.write({ body }),
    invalidates: { screen: 'scr03', resources: ['integrity'] },
  });
  const timeZone =
    context.plants.find((plant) => plant.plant_id === accepted?.chain.plant_id)?.timezone ??
    scopeZone;
  return (
    <main className={styles.page}>
      <h1>{text('scr03.integrity')}</h1>
      <p>{text('scr03.no_packages')}</p>
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          setAccepted(null);
          const body: VerifyRequest =
            chain === 'audit'
              ? { kind: 'audit' }
              : chain === 'ledger'
                ? { kind: 'ledger' }
                : { kind: 'ledger', plant_id: chain };
          void verification
            .run(body)
            .then(setAccepted)
            .catch(() => undefined);
        }}
      >
        <label>
          {text('scr03.chain')}
          <select
            disabled={verification.status === 'pending'}
            value={chain}
            onChange={(event) => {
              setChain(event.target.value);
              setAccepted(null);
              verification.reset();
            }}
          >
            <option value="ledger">{text('scr03.organization_ledger')}</option>
            <option value="audit">{text('scr03.organization_audit')}</option>
            {context.plants.map((plant) => (
              <option key={plant.plant_id} value={plant.plant_id}>
                {plant.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={verification.status === 'pending'}>
          {text('scr03.verify')}
        </button>
      </form>
      <IntegrityQueryState
        query={{
          status: verification.status,
          error: verification.error,
          isFetching: verification.status === 'pending',
        }}
      >
        {accepted === null ? null : (
          <p role="status">
            {text('scr03.queued')}{' '}
            <ResultDate instant={accepted.requested_at} timeZone={timeZone} />
          </p>
        )}
      </IntegrityQueryState>
      <section aria-label={text('scr03.history')}>
        <h2>{text('scr03.history')}</h2>
        <p>{text('scr03.latest_only')}</p>
        {historyOpen ? (
          <ResultsTable
            key={`${context.organizationId}:${context.concession ?? ''}`}
            plants={context.plants}
          />
        ) : (
          <button
            type="button"
            onClick={() => {
              setHistoryOpen(true);
            }}
          >
            {text('components_status.refresh.button')}
          </button>
        )}
      </section>
      <section aria-label={text('scr03.checkpoints')}>
        <h2>{text('scr03.checkpoints')}</h2>
        <IntegrityRefresh query={checkpoints} />
        <IntegrityQueryState query={checkpoints}>
          {checkpoints.data === undefined ? null : (
            <CheckpointsView
              {...checkpoints.data}
              plants={context.plants}
              timeZone={scopeZone}
              organizationId={context.organizationId}
              concessionOrganizationId={context.concessionOrganizationId}
            />
          )}
        </IntegrityQueryState>
      </section>
      <section aria-label={text('scr03.verifier')}>
        <h2>{text('scr03.verifier')}</h2>
        <IntegrityRefresh query={verifier} />
        <IntegrityQueryState query={verifier}>
          {verifier.data === undefined ? null : <VerifierView sha256={verifier.data.sha256} />}
        </IntegrityQueryState>
      </section>
      <nav aria-label={text('scr03.coverage')}>
        {context.plants.flatMap((plant) =>
          plant.zones.map((zone) => (
            <p key={zone.zone_id}>
              <Link to={`/zones/${zone.zone_id}/coverage`}>
                {text('scr03.coverage_link')}: {zone.name}
              </Link>
            </p>
          )),
        )}
      </nav>
    </main>
  );
}

export default function Page() {
  const { principal } = useUiSession();
  return (
    <Integrity
      key={`${principal?.organization.organization_id ?? ''}:${principal?.concession_id ?? ''}`}
    />
  );
}

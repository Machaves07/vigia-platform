/**
 * RV-08 · Compuertas y acuerdos (IF-11): estado de montaje y uso con su fecha, modo resultante,
 * firmantes configurados, firmas con fecha y política de planta. Pinta solo esos campos: ningún
 * hallazgo ni contador, aunque la respuesta los traiga. Los identificadores sirven para relacionar
 * datos y nunca se muestran (`decided_by`, `record_id`, `user_id`).
 */
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  gateDisplay,
  type Agreement,
  type GateState,
  type PlantPolicy,
  type SignatoryPolicy,
} from '@/ports/scr05Gates';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import styles from './scr05.module.css';

function Instant({ instant, timeZone }: { readonly instant: string; readonly timeZone: string }) {
  return <TimestampWithOffset instant={instant} timeZone={timeZone} />;
}

/** Montaje, uso, modo resultante y emisión del sobre de compuertas vigente. */
export function GateStatePanel({
  gates,
  timeZone,
}: {
  readonly gates: Pick<GateState, 'mounting' | 'usage' | 'resulting_mode' | 'issued_at'>;
  readonly timeZone: string;
}) {
  const rows = [
    { key: 'scr05.gates.mounting', gate: gates.mounting },
    { key: 'scr05.gates.usage', gate: gates.usage },
  ];
  return (
    <div className={styles.section} data-part="gate_state">
      <GateStateChip gate={gateDisplay(gates)} timeZone={timeZone} />
      <dl className={styles.facts}>
        {rows.map(({ key, gate }) => (
          <div key={key} className={styles.fact}>
            <dt>{text(key)}</dt>
            <dd>
              {label('gate_status', gate.status)}
              {' · '}
              {gate.decided_at === null ? (
                text('scr05.gates.undecided')
              ) : (
                <>
                  {text('scr05.gates.decided_at')}{' '}
                  <Instant instant={gate.decided_at} timeZone={timeZone} />
                </>
              )}
            </dd>
          </div>
        ))}
        <div className={styles.fact}>
          <dt>{text('scr05.gates.resulting_mode')}</dt>
          <dd data-part="resulting_mode">{label('zone_mode', gates.resulting_mode)}</dd>
        </div>
        <div className={styles.fact}>
          <dt>{text('scr05.gates.issued_at')}</dt>
          <dd>
            {gates.issued_at === null ? (
              text('scr05.gates.never_issued')
            ) : (
              <Instant instant={gates.issued_at} timeZone={timeZone} />
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}

/** Un acuerdo de uso: estado, fechas y firmantes con la fecha de su confirmación. */
export function AgreementView({
  agreement,
  timeZone,
  caption,
}: {
  readonly agreement: Agreement;
  readonly timeZone: string;
  readonly caption: string;
}) {
  return (
    <div className={styles.section} data-part="agreement">
      <dl className={styles.facts}>
        <div className={styles.fact}>
          <dt>{text('scr05.agreement.status')}</dt>
          <dd data-part="agreement_status">{label('agreement_status', agreement.status)}</dd>
        </div>
        <div className={styles.fact}>
          <dt>{text('scr05.agreement.created_at')}</dt>
          <dd>
            <Instant instant={agreement.created_at} timeZone={timeZone} />
          </dd>
        </div>
        <div className={styles.fact}>
          <dt>{text('scr05.agreement.approved_at')}</dt>
          <dd>
            {agreement.approved_at === null ? (
              text('scr05.agreement.not_approved')
            ) : (
              <Instant instant={agreement.approved_at} timeZone={timeZone} />
            )}
          </dd>
        </div>
        <div className={styles.fact}>
          <dt>{text('scr05.agreement.document')}</dt>
          <dd>
            {text(
              agreement.document_sha256 === null
                ? 'scr05.agreement.no_document'
                : 'scr05.agreement.with_document',
            )}
          </dd>
        </div>
      </dl>
      <div className={styles.scroll}>
        <table>
          <caption>{caption}</caption>
          <thead>
            <tr>
              <th scope="col">{text('scr05.agreement.role')}</th>
              <th scope="col">{text('scr05.agreement.signatory')}</th>
              <th scope="col">{text('scr05.agreement.confirmed_at')}</th>
            </tr>
          </thead>
          <tbody>
            {agreement.signatories.map((signatory) => (
              <tr key={`${signatory.user_id}:${signatory.role}`}>
                <td>{label('role', signatory.role)}</td>
                <td>{signatory.display_name ?? text('scr05.agreement.unnamed')}</td>
                <td>
                  {signatory.confirmed_at === null ? (
                    text('scr05.agreement.pending')
                  ) : (
                    <Instant instant={signatory.confirmed_at} timeZone={timeZone} />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Política de firmantes de la planta: roles exigidos, mínimo y última modificación. */
export function SignatoryPolicyView({
  policy,
  timeZone,
}: {
  readonly policy: SignatoryPolicy;
  readonly timeZone: string;
}) {
  if (!policy.configured) return <p>{text('scr05.signatories.not_configured')}</p>;
  const roles = policy.required_roles ?? [];
  return (
    <dl className={styles.facts} data-part="signatory_policy">
      <div className={styles.fact}>
        <dt>{text('scr05.signatories.required_roles')}</dt>
        <dd>
          {roles.length === 0 ? (
            text('scr05.signatories.no_roles')
          ) : (
            <ul>
              {roles.map((role) => (
                <li key={role}>{label('role', role)}</li>
              ))}
            </ul>
          )}
        </dd>
      </div>
      <div className={styles.fact}>
        <dt>{text('scr05.signatories.minimum')}</dt>
        <dd>
          {policy.minimum === null || policy.minimum === undefined ? '—' : String(policy.minimum)}
        </dd>
      </div>
      <div className={styles.fact}>
        <dt>{text('scr05.signatories.updated_at')}</dt>
        <dd>
          {policy.updated_at === null || policy.updated_at === undefined ? (
            '—'
          ) : (
            <Instant instant={policy.updated_at} timeZone={timeZone} />
          )}
        </dd>
      </div>
    </dl>
  );
}

/** Política de incerrables vigente de la planta (versión, fecha, firmante y concepto legal). */
export function PlantPolicyView({
  policy,
  timeZone,
}: {
  readonly policy: PlantPolicy;
  readonly timeZone: string;
}) {
  if (!policy.loaded) return <p>{text('scr05.policy.not_loaded')}</p>;
  return (
    <dl className={styles.facts} data-part="plant_policy">
      <div className={styles.fact}>
        <dt>{text('scr05.policy.version')}</dt>
        <dd>
          {policy.version === null || policy.version === undefined ? '—' : String(policy.version)}
        </dd>
      </div>
      <div className={styles.fact}>
        <dt>{text('scr05.policy.signed_at')}</dt>
        <dd>
          {policy.signed_at === null || policy.signed_at === undefined ? (
            '—'
          ) : (
            <Instant instant={policy.signed_at} timeZone={timeZone} />
          )}
        </dd>
      </div>
      <div className={styles.fact}>
        <dt>{text('scr05.policy.signed_by')}</dt>
        <dd>{policy.signed_by_display_name ?? '—'}</dd>
      </div>
      <div className={styles.fact}>
        <dt>{text('scr05.policy.legal_opinion')}</dt>
        <dd>{policy.legal_opinion_reference ?? '—'}</dd>
      </div>
      <div className={styles.fact}>
        <dt>{text('scr05.policy.criteria')}</dt>
        <dd className={styles.declared}>{policy.criteria_summary_es ?? '—'}</dd>
      </div>
    </dl>
  );
}

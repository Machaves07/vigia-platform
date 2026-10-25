/**
 * Revocación de una compuerta, montaje o uso (IF-11 paso 6, BR-APP-31): motivo obligatorio
 * (UI-13, 10 a 500 caracteres de U-03) y confirmación explícita en un segundo paso; nada se envía
 * sin las dos. El servidor recalcula el modo resultante y la pantalla muestra lo que devuelve.
 */
import { useId, useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import {
  gatesNotice,
  gatesPort,
  REVOCATION_REASON_LIMITS,
  type GateKind,
} from '@/ports/scr05Gates';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { WriteFailure } from '../shared';
import styles from '../scr05.module.css';

const GATES: readonly GateKind[] = ['mounting', 'usage'];

interface RevocationInput {
  readonly gate: GateKind;
  readonly reason: string;
}

export function Revocation({ zoneId }: { readonly zoneId: string }) {
  const id = useId();
  const [gate, setGate] = useState<GateKind>('usage');
  const [reason, setReason] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const mutation = useScreenMutation({
    write: (input: RevocationInput) =>
      gatesPort.revoke.write({
        path: { zone_id: zoneId, gate: input.gate },
        body: { reason_es: input.reason },
      }),
    invalidates: { screen: 'scr05', resources: ['gates', 'agreements', 'transparency'] },
  });
  // Solo el rechazo del texto se ancla al motivo; el resto (compuerta no aprobada) va al formulario.
  const failure = gatesNotice(mutation.error, 'reason_es');
  const reasonNotice =
    failure?.error.detail_code === 'catalog_free_text_rejected' ? failure : undefined;
  return (
    <section className={styles.panel} aria-labelledby={`${id}-title`} data-part="revocation">
      <h2 id={`${id}-title`}>{text('scr05.revocation.title')}</h2>
      <p>{text('scr05.revocation.intro')}</p>
      <form
        noValidate
        className={styles.section}
        onSubmit={(event) => {
          event.preventDefault();
          setShowErrors(true);
          if (reasonIssueKey(reason, REVOCATION_REASON_LIMITS) !== null) return;
          mutation.reset();
          setConfirming(true);
        }}
      >
        <fieldset className={styles.field} disabled={confirming}>
          <legend>{text('scr05.revocation.gate')}</legend>
          {GATES.map((kind) => (
            <label key={kind} className={styles.inline}>
              <input
                type="radio"
                name={`${id}-gate`}
                value={kind}
                checked={gate === kind}
                onChange={() => {
                  setGate(kind);
                }}
              />
              {text(`scr05.gates.${kind}`)}
            </label>
          ))}
        </fieldset>
        <ReasonField
          label={text('scr05.revocation.reason')}
          value={reason}
          onChange={(value) => {
            setReason(value);
            setConfirming(false);
          }}
          limits={REVOCATION_REASON_LIMITS}
          name="reason_es"
          showErrors={showErrors}
          notice={reasonNotice}
        />
        {confirming ? null : (
          <div className={styles.actions}>
            <button type="submit" className={styles.button}>
              {text('scr05.revocation.request')}
            </button>
          </div>
        )}
      </form>
      {confirming ? (
        <div
          className={styles.panel}
          role="group"
          aria-labelledby={`${id}-confirm`}
          data-part="revocation_confirm"
        >
          <h3 id={`${id}-confirm`}>{text('scr05.revocation.confirm_title')}</h3>
          <p>{text(`scr05.revocation.confirm_${gate}`)}</p>
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.button}
              disabled={mutation.status === 'pending'}
              onClick={() => {
                void mutation.run({ gate, reason }).then(
                  () => {
                    setConfirming(false);
                    setReason('');
                    setShowErrors(false);
                  },
                  () => undefined,
                );
              }}
            >
              {text('scr05.revocation.confirm')}
            </button>
            <button
              type="button"
              className={styles.button}
              disabled={mutation.status === 'pending'}
              onClick={() => {
                setConfirming(false);
              }}
            >
              {text('scr05.revocation.cancel')}
            </button>
          </div>
        </div>
      ) : null}
      {mutation.status === 'success' ? (
        <p role="status" data-part="revoked">
          {text('scr05.revocation.done')}
        </p>
      ) : null}
      {reasonNotice === undefined ? (
        <WriteFailure error={mutation.error} field="revocation" />
      ) : null}
    </section>
  );
}

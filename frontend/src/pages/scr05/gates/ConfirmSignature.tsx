/**
 * «Confirmar mi firma» desde SCR-05 (IF-11 paso 5, BR-APP-52): solo existe cuando el servidor
 * responde `pending_confirmation_for_me` para la persona autenticada, y envía **ese**
 * `agreement_id` sin cuerpo: el servidor confirma la firma del usuario de la sesión y nadie puede
 * firmar en nombre de otro. La pantalla nunca elige ni envía un `user_id`.
 *
 * El resumen es lo que se acuerda: el alcance declarado y los estándares de la zona (la respuesta
 * no trae otro detalle del acuerdo pendiente).
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import { gatesPort, type Confirmation, type Transparency } from '@/ports/scr05Gates';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { WriteFailure } from '../shared';
import styles from '../scr05.module.css';

/** Resumen del acuerdo pendiente: alcance declarado y estándares de la zona. */
export function ConfirmSummary({ transparency }: { readonly transparency: Transparency }) {
  const scope = transparency.declared_scope.scope_text_es;
  return (
    <>
      <p>{text('scr05.confirm.summary')}</p>
      <h3>{text('scr05.confirm.scope')}</h3>
      <p className={styles.declared}>{scope ?? text('scr05.confirm.no_scope')}</p>
      <h3>{text('scr05.confirm.standards')}</h3>
      {transparency.standards.length === 0 ? (
        <p>{text('scr05.confirm.no_standards')}</p>
      ) : (
        <ul>
          {transparency.standards.map((standard) => (
            <li key={`${standard.standard_id}:${String(standard.version)}`}>{standard.title_es}</li>
          ))}
        </ul>
      )}
      <p>{text('scr05.confirm.own_session')}</p>
    </>
  );
}

export function ConfirmSignature({
  agreementId,
  transparency,
  onConfirmed,
}: {
  readonly agreementId: string;
  readonly transparency: Transparency;
  /** Recibe el acuse: la pantalla lo conserva aunque el refresco retire esta acción. */
  readonly onConfirmed: (confirmation: Confirmation) => void;
}) {
  const mutation = useScreenMutation({
    write: (agreement_id: string) => gatesPort.confirm.write({ path: { agreement_id } }),
    invalidates: { screen: 'scr05', resources: ['agreements', 'gates', 'transparency'] },
  });
  return (
    <section className={styles.panel} aria-labelledby="scr05-confirm" data-part="confirm">
      <h2 id="scr05-confirm">{text('scr05.confirm.title')}</h2>
      <ConfirmSummary transparency={transparency} />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void mutation.run(agreementId).then(onConfirmed, () => undefined);
        }}
      >
        <button
          type="submit"
          className={styles.button}
          disabled={mutation.status === 'pending' || mutation.status === 'success'}
        >
          {text('scr05.confirm.action')}
        </button>
      </form>
      <WriteFailure error={mutation.error} field="confirmation" />
    </section>
  );
}

/** Acuse de la confirmación con el `role_in_use` y el momento que devolvió el servidor. */
export function ConfirmReceipt({
  receipt,
  timeZone,
}: {
  readonly receipt: Confirmation;
  readonly timeZone: string;
}) {
  return (
    <section className={styles.section} aria-labelledby="scr05-confirmed" data-part="confirmed">
      <h2 id="scr05-confirmed">{text('scr05.confirm.done')}</h2>
      <p>
        {text('scr05.confirm.receipt')}{' '}
        <TimestampWithOffset instant={receipt.confirmed_at} timeZone={timeZone} />
      </p>
      <WriteReceipt record={text('scr05.confirm.done')} roleInUse={receipt.role_in_use} />
    </section>
  );
}

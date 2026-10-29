/**
 * «Confirmar mi firma» desde SCR-16 (IF-19 paso 2, BR-APP-52): solo existe cuando la respuesta de
 * transparencia trae `pending_confirmation_for_me`, y envía **ese** `agreement_id`, sin cuerpo; el
 * servidor confirma la firma del usuario de la sesión y nadie puede firmar en nombre de otro. El
 * resumen es el alcance declarado y los estándares de la zona, que son lo que se acuerda (la
 * respuesta no trae otro detalle del acuerdo pendiente).
 */
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import {
  confirmNotice,
  transparencyFailure,
  transparencyPort,
  type Confirmation,
  type Transparency,
} from '@/ports/scr16Transparency';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import styles from '../scr16.module.css';

const NOTICE_ID = 'scr16-confirm-notice';

/** Resumen de lo que se acuerda, sin ningún dato de gestión (RV-20). */
export function ConfirmSummary({ transparency }: { readonly transparency: Transparency }) {
  const scope = transparency.declared_scope.scope_text_es;
  return (
    <>
      <p>{text('scr16.confirm.summary')}</p>
      <h3>{text('scr16.confirm.scope')}</h3>
      <p className={styles.declared}>{scope ?? text('scr16.scope.no_record')}</p>
      <h3>{text('scr16.confirm.standards')}</h3>
      {transparency.standards.length === 0 ? (
        <p>{text('scr16.standards.empty')}</p>
      ) : (
        <ul>
          {transparency.standards.map((standard) => (
            <li key={`${standard.standard_id}:${String(standard.version)}`}>{standard.title_es}</li>
          ))}
        </ul>
      )}
      <p>{text('scr16.confirm.own_session')}</p>
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
    write: (agreement_id: string) => transparencyPort.confirm.write({ path: { agreement_id } }),
    invalidates: { screen: 'scr16', resources: ['transparency', 'agreements', 'gates'] },
  });
  const notice = confirmNotice(mutation.error);
  return (
    <section className={styles.confirm} aria-labelledby="scr16-confirm" data-part="confirm">
      <h2 id="scr16-confirm">{text('scr16.confirm.title')}</h2>
      <ConfirmSummary transparency={transparency} />
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void mutation.run(agreementId).then(onConfirmed, () => undefined);
        }}
        aria-describedby={notice === null ? undefined : NOTICE_ID}
      >
        <button
          type="submit"
          className={styles.button}
          disabled={mutation.status === 'pending' || mutation.status === 'success'}
        >
          {text('scr16.confirm.action')}
        </button>
      </form>
      {notice === null ? null : <FieldNotice id={NOTICE_ID} notice={notice} />}
      {mutation.status === 'error' && notice === null ? (
        <p role="alert">
          {text(
            mutation.error !== null && transparencyFailure(mutation.error).state === 'unavailable'
              ? 'screen_state.unavailable'
              : 'screen_state.error',
          )}
        </p>
      ) : null}
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
    <section className={styles.section} aria-labelledby="scr16-confirmed" data-part="confirmed">
      <h2 id="scr16-confirmed">{text('scr16.confirm.done')}</h2>
      <p>
        {text('scr16.confirm.receipt')}{' '}
        <TimestampWithOffset instant={receipt.confirmed_at} timeZone={timeZone} />
      </p>
      <WriteReceipt record={text('scr16.confirm.done')} roleInUse={receipt.role_in_use} />
    </section>
  );
}

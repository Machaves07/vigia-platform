/**
 * `/login/enroll` (IF-02 desde IF-01): `POST /auth/second-factor/enroll` sin código devuelve el
 * QR y los diez códigos de recuperación, que se muestran **una vez** con «lo guardé» obligatorio;
 * después, el primer TOTP confirma con el mismo cuerpo y `code`. Un código rechazado repite solo
 * ese paso: nunca se vuelve a pedir la inscripción, que regeneraría el secreto (BR-APP-12).
 *
 * El QR, el secreto y los códigos viven solo en el estado de esta página: ni caché de consultas,
 * ni almacenamiento, ni URL. Los códigos se sueltan al pulsar «lo guardé»; el resto, al confirmar
 * o al salir de la ruta (la página se desmonta).
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';

import {
  sessionFailure,
  sessionScreenPort,
  type EnrollmentView,
  type SessionFailure,
} from '@/ports/scr01Session';
import { ScreenState } from '@/pages/shared/ScreenState';
import { useSession } from '@/state/session';
import { text } from '@/texts';

import { QrCode, RecoveryCodes } from '../EnrollmentSecrets';
import styles from '../scr01.module.css';
import { completeSignIn, FailureNotice, TOTP, useThrottle } from '../session';

type Start =
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'ready'; readonly enrollment: EnrollmentView }
  /** Factor confirmado: falta cargar la identidad (`GET /me`). */
  | { readonly kind: 'confirmed' };

export function Enroll(): ReactNode {
  const session = useSession();
  const navigate = useNavigate();
  const id = useId();
  const throttle = useThrottle();
  const [start, setStart] = useState<Start>({ kind: 'loading' });
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const [code, setCode] = useState('');
  const [checked, setChecked] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<SessionFailure | null>(null);
  // Una sola inscripción por visita: el doble montaje de StrictMode no la repite.
  const requested = useRef(false);

  useEffect(() => {
    if (requested.current) return;
    requested.current = true;
    sessionScreenPort.enroll.write({ body: {} }).then(
      (response) => {
        const enrollment = response.enrollment;
        if (response.status !== 'enrollment_started' || enrollment == null) {
          setStart({ kind: 'failed' });
          return;
        }
        setCodes(enrollment.recovery_codes);
        setStart({ kind: 'ready', enrollment });
      },
      () => {
        setStart({ kind: 'failed' });
      },
    );
  }, []);

  const codeOk = TOTP.test(code.trim());
  const codeError = checked && !codeOk;

  const confirm = async () => {
    setChecked(true);
    if (!codeOk || throttle.blocked || pending) return;
    setPending(true);
    setFailure(null);
    try {
      await sessionScreenPort.enroll.write({ body: { code: code.trim() } });
    } catch (error) {
      const classified = sessionFailure(error);
      if (classified.kind === 'throttled') throttle.start(classified.retryAfterSeconds);
      setCode('');
      setFailure(classified);
      setPending(false);
      return;
    }
    // Inscrito: el QR y el secreto se sueltan antes de seguir.
    setCode('');
    setStart({ kind: 'confirmed' });
    try {
      await completeSignIn(session, navigate);
    } catch (error) {
      setFailure(sessionFailure(error));
    } finally {
      setPending(false);
    }
  };

  const backToLogin = (
    <button
      type="button"
      className={styles.button}
      onClick={() => {
        void session.logout();
      }}
    >
      {text('scr01.back_to_login')}
    </button>
  );

  return (
    <main className={styles.page} aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} className={styles.title}>
        {text('scr01.enroll.title')}
      </h1>
      {start.kind === 'loading' || (start.kind === 'confirmed' && failure === null) ? (
        <ScreenState state="loading" />
      ) : null}
      {start.kind === 'confirmed' && failure !== null ? (
        <>
          <FailureNotice failure={failure} invalidKey="scr01.error" remaining={0} />
          <div className={styles.actions}>{backToLogin}</div>
        </>
      ) : null}
      {start.kind === 'failed' ? (
        <>
          <p className={styles.error} role="alert" data-text-key="scr01.enroll.start_failed">
            {text('scr01.enroll.start_failed')}
          </p>
          <div className={styles.actions}>{backToLogin}</div>
        </>
      ) : null}
      {start.kind === 'ready' ? (
        <>
          <p>{text('scr01.enroll.intro')}</p>
          <QrCode enrollment={start.enrollment} />
          <RecoveryCodes
            codes={codes}
            onAcknowledged={() => {
              setCodes(null);
            }}
          />
          {codes === null ? (
            <form
              className={styles.form}
              noValidate
              aria-labelledby={`${id}-confirm`}
              onSubmit={(event) => {
                event.preventDefault();
                void confirm();
              }}
            >
              <h2 id={`${id}-confirm`}>{text('scr01.enroll.confirm_title')}</h2>
              <div className={styles.field}>
                <label htmlFor={`${id}-code`}>{text('scr01.enroll.code')}</label>
                <input
                  id={`${id}-code`}
                  className={styles.input}
                  name="code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  required
                  value={code}
                  aria-invalid={codeError}
                  aria-describedby={codeError ? `${id}-code-error` : undefined}
                  onChange={(event) => {
                    setCode(event.currentTarget.value);
                  }}
                />
                {codeError ? (
                  <p id={`${id}-code-error`} className={styles.error}>
                    {text('scr01.enroll.code_invalid')}
                  </p>
                ) : null}
              </div>
              <FailureNotice
                failure={failure}
                invalidKey="scr01.invalid_credentials"
                remaining={throttle.remaining}
              />
              <div className={styles.actions}>
                <button
                  type="submit"
                  className={styles.button}
                  disabled={pending || throttle.blocked}
                >
                  {text('scr01.enroll.confirm')}
                </button>
                {backToLogin}
              </div>
            </form>
          ) : (
            <div className={styles.actions}>{backToLogin}</div>
          )}
        </>
      ) : null}
    </main>
  );
}

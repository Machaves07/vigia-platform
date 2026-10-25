/**
 * SCR-01, grupo `invitation` (IF-04, BR-APP-18; público, VIG-321): `/invitations/{token}`.
 *
 * `POST /invitations/{token}/accept` con `step: 'begin'` valida el enlace y trae el aviso vigente
 * y, si el rol lo exige, la inscripción del segundo factor; después, un `StepWizard` con la
 * contraseña (estructura validada), el segundo factor (QR, códigos una vez y primer TOTP) y la
 * aceptación del aviso, y `step: 'complete'` activa la cuenta. Un enlace usado, vencido o
 * inexistente (`not_found`) pinta siempre el mismo mensaje del catálogo.
 *
 * El token solo vive en la ruta y en las propiedades de esta página: no se guarda ni se registra
 * (el registro de desarrollo lleva la plantilla `/invitations/{token}/accept`). La inscripción se
 * pide una sola vez por visita, porque cada `begin` regenera el secreto.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router';

import { StepWizard } from '@/components/StepWizard/StepWizard';
import {
  sessionFailure,
  sessionScreenPort,
  type ActivationResponse,
  type SessionFailure,
} from '@/ports/scr01Session';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';

import { QrCode, RecoveryCodes } from '../EnrollmentSecrets';
import { NoticeBody } from '../privacyNotice';
import styles from '../scr01.module.css';
import { FailureNotice, PASSWORD_MIN_LENGTH, TOTP, useThrottle } from '../session';

type Begin =
  | { readonly kind: 'loading' }
  | { readonly kind: 'invalid' }
  | { readonly kind: 'failed'; readonly failure: SessionFailure }
  | { readonly kind: 'ready'; readonly response: ActivationResponse }
  | { readonly kind: 'activated' };

function InvalidLink(): ReactNode {
  return (
    <>
      <p className={styles.error} role="alert" data-text-key="scr01.invitation.invalid">
        {text('scr01.invitation.invalid')}
      </p>
      <Link to="/login">{text('scr01.invitation.go_login')}</Link>
    </>
  );
}

function Field({
  id,
  label,
  error,
  children,
}: {
  readonly id: string;
  readonly label: string;
  readonly error: string | null;
  readonly children: ReactNode;
}): ReactNode {
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      {children}
      {error === null ? null : (
        <p id={`${id}-error`} className={styles.error}>
          {error}
        </p>
      )}
    </div>
  );
}

function Wizard({
  token,
  response,
  onInvalid,
  onActivated,
}: {
  readonly token: string;
  readonly response: ActivationResponse;
  readonly onInvalid: () => void;
  readonly onActivated: () => void;
}): ReactNode {
  const id = useId();
  const throttle = useThrottle();
  const [password, setPassword] = useState('');
  const [repeated, setRepeated] = useState('');
  const [codes, setCodes] = useState<readonly string[] | null>(
    response.enrollment?.recovery_codes ?? null,
  );
  const [code, setCode] = useState('');
  const [accepted, setAccepted] = useState(false);
  const [failure, setFailure] = useState<SessionFailure | null>(null);

  const enrollment = response.enrollment ?? null;
  const notice = response.notice ?? null;
  // Sin inscripción en la respuesta el factor ya quedó confirmado en un intento anterior.
  const needsCode = response.second_factor_required === true && enrollment !== null;
  const passwordOk = password.length >= PASSWORD_MIN_LENGTH && password === repeated;
  const codeOk = codes === null && TOTP.test(code.trim());

  const passwordError =
    password !== '' && password.length < PASSWORD_MIN_LENGTH
      ? text('scr01.login.password_short')
      : null;
  const repeatedError =
    repeated !== '' && repeated !== password ? text('scr01.invitation.password_mismatch') : null;

  const steps = [
    {
      id: 'password',
      title: text('scr01.invitation.step_password'),
      complete: passwordOk,
      content: (
        <div className={styles.form}>
          <Field
            id={`${id}-password`}
            label={text('scr01.invitation.password')}
            error={passwordError}
          >
            <input
              id={`${id}-password`}
              className={styles.input}
              type="password"
              autoComplete="new-password"
              value={password}
              aria-invalid={passwordError !== null}
              aria-describedby={passwordError === null ? undefined : `${id}-password-error`}
              onChange={(event) => {
                setPassword(event.currentTarget.value);
              }}
            />
          </Field>
          <Field
            id={`${id}-repeated`}
            label={text('scr01.invitation.password_confirm')}
            error={repeatedError}
          >
            <input
              id={`${id}-repeated`}
              className={styles.input}
              type="password"
              autoComplete="new-password"
              value={repeated}
              aria-invalid={repeatedError !== null}
              aria-describedby={repeatedError === null ? undefined : `${id}-repeated-error`}
              onChange={(event) => {
                setRepeated(event.currentTarget.value);
              }}
            />
          </Field>
        </div>
      ),
    },
    ...(needsCode
      ? [
          {
            id: 'second_factor',
            title: text('scr01.invitation.step_second_factor'),
            complete: codeOk,
            content: (
              <div className={styles.form}>
                <p>{text('scr01.enroll.intro')}</p>
                <QrCode enrollment={enrollment} />
                <RecoveryCodes
                  codes={codes}
                  onAcknowledged={() => {
                    setCodes(null);
                  }}
                />
                {codes === null ? (
                  <Field id={`${id}-code`} label={text('scr01.enroll.code')} error={null}>
                    <input
                      id={`${id}-code`}
                      className={styles.input}
                      type="text"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      value={code}
                      onChange={(event) => {
                        setCode(event.currentTarget.value);
                      }}
                    />
                  </Field>
                ) : null}
              </div>
            ),
          },
        ]
      : []),
    {
      id: 'notice',
      title: text('scr01.invitation.step_notice'),
      complete: accepted && notice !== null,
      content:
        notice === null ? (
          <p className={styles.error} role="alert">
            {text('scr01.privacy.text_unavailable')}
          </p>
        ) : (
          <div className={styles.form}>
            <p data-part="version">
              {text('scr01.privacy.version')}: <strong>{notice.version}</strong>
            </p>
            <section className={styles.notice} aria-label={text('scr01.privacy.title')}>
              <NoticeBody body={notice.text} />
            </section>
            <label>
              <input
                type="checkbox"
                checked={accepted}
                onChange={(event) => {
                  setAccepted(event.currentTarget.checked);
                }}
              />{' '}
              {text('scr01.invitation.notice_accept')}
            </label>
          </div>
        ),
    },
  ];

  const submit = async () => {
    if (notice === null || throttle.blocked) throw new Error('Activación incompleta.');
    setFailure(null);
    try {
      await sessionScreenPort.acceptInvitation.write({
        path: { token },
        body: {
          step: 'complete',
          password,
          notice_version: notice.version,
          ...(needsCode ? { second_factor_code: code.trim() } : {}),
        },
      });
    } catch (error) {
      const classified = sessionFailure(error);
      if (classified.kind === 'not_found') {
        onInvalid();
      } else {
        if (classified.kind === 'throttled') throttle.start(classified.retryAfterSeconds);
        setFailure(classified);
      }
      throw error;
    }
    setPassword('');
    setRepeated('');
    setCode('');
    onActivated();
  };

  return (
    <>
      <StepWizard
        title={text('scr01.invitation.title')}
        dirty={password !== '' || repeated !== ''}
        steps={steps}
        summary={<p>{text('scr01.invitation.summary')}</p>}
        onSubmit={submit}
      />
      <FailureNotice
        failure={failure}
        invalidKey="scr01.invitation.rejected"
        remaining={throttle.remaining}
      />
    </>
  );
}

function Activation({ token }: { readonly token: string }): ReactNode {
  const id = useId();
  const [begin, setBegin] = useState<Begin>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  // Una sola petición por intento: el doble montaje de StrictMode no regenera la inscripción.
  const requested = useRef(-1);

  useEffect(() => {
    if (requested.current === attempt) return;
    requested.current = attempt;
    sessionScreenPort.acceptInvitation.write({ path: { token }, body: { step: 'begin' } }).then(
      (response) => {
        setBegin({ kind: 'ready', response });
      },
      (error: unknown) => {
        const failure = sessionFailure(error);
        setBegin(failure.kind === 'not_found' ? { kind: 'invalid' } : { kind: 'failed', failure });
      },
    );
  }, [token, attempt]);

  return (
    <main className={styles.page} aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} className={styles.title}>
        {text('scr01.invitation.title')}
      </h1>
      {begin.kind === 'loading' ? <ScreenState state="loading" /> : null}
      {begin.kind === 'invalid' ? <InvalidLink /> : null}
      {begin.kind === 'failed' ? (
        <>
          <FailureNotice
            failure={begin.failure}
            invalidKey="scr01.invitation.invalid"
            remaining={0}
          />
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.button}
              onClick={() => {
                setBegin({ kind: 'loading' });
                setAttempt((value) => value + 1);
              }}
            >
              {text('screen_state.retry')}
            </button>
          </div>
        </>
      ) : null}
      {begin.kind === 'ready' ? (
        <Wizard
          token={token}
          response={begin.response}
          onInvalid={() => {
            setBegin({ kind: 'invalid' });
          }}
          onActivated={() => {
            setBegin({ kind: 'activated' });
          }}
        />
      ) : null}
      {begin.kind === 'activated' ? (
        <>
          <p role="status" data-text-key="scr01.invitation.activated">
            {text('scr01.invitation.activated')}
          </p>
          <Link to="/login">{text('scr01.invitation.go_login')}</Link>
        </>
      ) : null}
    </main>
  );
}

export default function Page(): ReactNode {
  const { token = '' } = useParams();
  return <Activation key={token} token={token} />;
}

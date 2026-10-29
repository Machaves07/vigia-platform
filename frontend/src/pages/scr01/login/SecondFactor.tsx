/**
 * `/login/second-factor` (IF-01, paso 3): TOTP de 6 dígitos o código de recuperación y
 * `POST /auth/second-factor`. Un código incorrecto, o la sesión pendiente vencida, pintan la misma
 * clave que las credenciales inválidas (BR-APP-10); `throttled`, cuenta regresiva (BR-APP-11).
 * «Volver» cierra la sesión pendiente en el servidor y vuelve a `/login` (BR-APP-17).
 */
import { useId, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';

import { sessionFailure, sessionScreenPort, type SessionFailure } from '@/ports/scr01Session';
import { useSession } from '@/state/session';
import { text } from '@/texts';

import styles from '../scr01.module.css';
import { AuthFrame } from './AuthFrame';
import { completeSignIn, FailureNotice, isSecondFactorCode, useThrottle } from '../session';

export function SecondFactor(): ReactNode {
  const session = useSession();
  const navigate = useNavigate();
  const id = useId();
  const throttle = useThrottle();
  const [code, setCode] = useState('');
  const [checked, setChecked] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<SessionFailure | null>(null);

  const codeOk = isSecondFactorCode(code);
  const codeError = checked && !codeOk;

  const submit = async () => {
    setChecked(true);
    if (!codeOk || throttle.blocked || pending) return;
    setPending(true);
    setFailure(null);
    try {
      await sessionScreenPort.secondFactor.write({ body: { code: code.trim() } });
      setCode('');
      await completeSignIn(session, navigate);
    } catch (error) {
      const classified = sessionFailure(error);
      if (classified.kind === 'throttled') throttle.start(classified.retryAfterSeconds);
      setCode('');
      setFailure(classified);
    } finally {
      setPending(false);
    }
  };

  return (
    <AuthFrame labelledBy={`${id}-title`}>
      <h1 id={`${id}-title`} className={styles.title}>
        {text('scr01.second_factor.title')}
      </h1>
      <form
        className={styles.form}
        noValidate
        aria-labelledby={`${id}-title`}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className={styles.field}>
          <label htmlFor={`${id}-code`}>{text('scr01.second_factor.code')}</label>
          <p id={`${id}-hint`} className={styles.hint}>
            {text('scr01.second_factor.hint')}
          </p>
          <input
            id={`${id}-code`}
            className={styles.input}
            name="code"
            type="text"
            autoComplete="one-time-code"
            spellCheck={false}
            required
            value={code}
            aria-invalid={codeError}
            aria-describedby={codeError ? `${id}-hint ${id}-code-error` : `${id}-hint`}
            onChange={(event) => {
              setCode(event.currentTarget.value);
            }}
          />
          {codeError ? (
            <p id={`${id}-code-error`} className={styles.error}>
              {text('scr01.second_factor.code_invalid')}
            </p>
          ) : null}
        </div>
        <FailureNotice
          failure={failure}
          invalidKey="scr01.invalid_credentials"
          remaining={throttle.remaining}
        />
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={pending || throttle.blocked}>
            {text('scr01.second_factor.submit')}
          </button>
          <button
            type="button"
            className={styles.button}
            onClick={() => {
              void session.logout();
            }}
          >
            {text('scr01.back_to_login')}
          </button>
        </div>
      </form>
    </AuthFrame>
  );
}

/**
 * `/login` (IF-01, pasos 1 y 2): correo y contraseña con validación estructural (BR-APP-42) y
 * `POST /auth/login`. Correo desconocido, contraseña incorrecta o cuenta inactiva pintan el mismo
 * árbol con la misma clave (BR-APP-10); `throttled` deshabilita el envío solo durante
 * `retry_after_seconds` (BR-APP-11). Con segundo factor, la única pantalla siguiente es la del
 * código o, si el servidor dice que no está inscrito, la de inscripción (BR-APP-09).
 */
import { useId, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';

import { sessionFailure, sessionScreenPort, type SessionFailure } from '@/ports/scr01Session';
import { useSession } from '@/state/session';
import { text } from '@/texts';

import styles from '../scr01.module.css';
import { completeSignIn, EMAIL, FailureNotice, PASSWORD_MIN_LENGTH, useThrottle } from '../session';

export function LoginForm(): ReactNode {
  const session = useSession();
  const navigate = useNavigate();
  const id = useId();
  const throttle = useThrottle();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [checked, setChecked] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<SessionFailure | null>(null);

  const emailOk = EMAIL.test(email.trim());
  const passwordOk = password.length >= PASSWORD_MIN_LENGTH;
  const emailError = checked && !emailOk;
  const passwordError = checked && !passwordOk;

  const submit = async () => {
    setChecked(true);
    if (!emailOk || !passwordOk || throttle.blocked || pending) return;
    setPending(true);
    setFailure(null);
    try {
      const response = await sessionScreenPort.login.write({
        body: { email: email.trim(), password },
      });
      setPassword('');
      if (response.status === 'authenticated') {
        await completeSignIn(session, navigate);
        return;
      }
      const enroll = response.status === 'second_factor_enrollment_required';
      session.secondFactorRequired(enroll);
      await navigate(enroll ? '/login/enroll' : '/login/second-factor', { replace: true });
    } catch (error) {
      const classified = sessionFailure(error);
      if (classified.kind === 'throttled') throttle.start(classified.retryAfterSeconds);
      setPassword('');
      setFailure(classified);
    } finally {
      setPending(false);
    }
  };

  return (
    <main className={styles.page} aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} className={styles.title}>
        {text('scr01.login.title')}
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
          <label htmlFor={`${id}-email`}>{text('scr01.login.email')}</label>
          <input
            id={`${id}-email`}
            className={styles.input}
            name="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            aria-invalid={emailError}
            aria-describedby={emailError ? `${id}-email-error` : undefined}
            onChange={(event) => {
              setEmail(event.currentTarget.value);
            }}
          />
          {emailError ? (
            <p id={`${id}-email-error`} className={styles.error}>
              {text('scr01.login.email_invalid')}
            </p>
          ) : null}
        </div>
        <div className={styles.field}>
          <label htmlFor={`${id}-password`}>{text('scr01.login.password')}</label>
          <input
            id={`${id}-password`}
            className={styles.input}
            name="password"
            type="password"
            autoComplete="current-password"
            required
            minLength={PASSWORD_MIN_LENGTH}
            value={password}
            aria-invalid={passwordError}
            aria-describedby={passwordError ? `${id}-password-error` : undefined}
            onChange={(event) => {
              setPassword(event.currentTarget.value);
            }}
          />
          {passwordError ? (
            <p id={`${id}-password-error`} className={styles.error}>
              {text('scr01.login.password_short')}
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
            {text('scr01.login.submit')}
          </button>
        </div>
      </form>
    </main>
  );
}

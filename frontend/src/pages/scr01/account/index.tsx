/**
 * SCR-01, grupo `account` (IF-02 e IF-04; RV-01; BR-APP-17; PAT-APP-MAN-01): datos de la cuenta
 * con sus asignaciones y alcances, sesiones abiertas con «cerrar las demás», cambio de contraseña
 * y las tres versiones para el soporte. Tras cambiar la contraseña la pantalla informa que las
 * demás sesiones quedaron cerradas. La contraseña solo vive en el estado del formulario y se
 * borra al aceptarse; ningún secreto ni identificador de sesión se pinta.
 */
import { useEffect, useId, useState } from 'react';

import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { ScreenState } from '@/pages/shared/ScreenState';
import {
  adminClock,
  adminPort,
  embeddedAppVersion,
  readFailure,
  readRelease,
  type ReleaseInfo,
} from '@/ports/scr01Admin';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

import { anchoredNoticeId, WriteNotices } from '../notices';
import { useScopeDirectory } from '../scopes';
import { AccountProfile, SessionsList } from './AccountView';

/** Longitud mínima de la contraseña que la interfaz comprueba antes de enviar (IF-01). */
const MIN_PASSWORD = 8;

/** Zona horaria del navegador: la cuenta no pertenece a una planta. */
function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

function Sessions({
  userId,
  concession,
}: {
  readonly userId: string;
  readonly concession: string | null;
}) {
  const sessions = useScreenQuery({
    key: screenKey({ screen: 'scr01', operation: 'GET /auth/sessions', scope: userId, concession }),
    policy: policyFor('GET /auth/sessions'),
    read: ({ signal }) => adminPort.sessions.read({}, signal),
  });
  const [closed, setClosed] = useState<number | null>(null);
  const closeOthers = useScreenMutation({
    write: () => adminPort.closeOthers.write(),
    invalidates: { screen: 'scr01', resources: ['sessions'] },
  });
  const closeId = useId();
  const others = (sessions.data?.sessions ?? []).filter((session) => !session.current).length;
  let body;
  if (sessions.status === 'pending') body = <ScreenState state="loading" />;
  else if (sessions.error !== null) {
    const failure = readFailure(sessions.error);
    body =
      failure.state === 'error' ? (
        <ScreenState
          state="error"
          {...(failure.correlationId === undefined ? {} : { correlationId: failure.correlationId })}
          onRetry={() => {
            void sessions.refresh();
          }}
        />
      ) : (
        <ScreenState state={failure.state} />
      );
  } else {
    body = <SessionsList sessions={sessions.data?.sessions ?? []} timeZone={browserTimeZone()} />;
  }
  return (
    <section aria-label={text('scr01_admin.sessions.title')}>
      <h2>{text('scr01_admin.sessions.title')}</h2>
      <RefreshIndicator
        clock={adminClock()}
        fetchedAt={sessions.fetchedAt}
        isFetching={sessions.isFetching}
        refreshPolicy="on_demand"
        onRefresh={() => {
          void sessions.refresh();
        }}
      />
      {body}
      {sessions.data !== undefined && others === 0 && closed === null && (
        <p>{text('scr01_admin.sessions.only_current')}</p>
      )}
      <button
        type="button"
        disabled={closeOthers.status === 'pending'}
        onClick={() => {
          setClosed(null);
          void closeOthers
            .run(undefined)
            .then((result) => {
              setClosed(result.sessions_closed);
            })
            .catch(() => undefined);
        }}
      >
        {text('scr01_admin.sessions.close_others')}
      </button>
      {closed !== null && (
        <p role="status" data-part="sessions_closed">
          {`${text('scr01_admin.sessions.closed')} ${String(closed)}`}
        </p>
      )}
      <WriteNotices error={closeOthers.error} formId={closeId} />
    </section>
  );
}

function PasswordForm() {
  const formId = useId();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const [changed, setChanged] = useState(false);
  const mutation = useScreenMutation({
    write: (body: { current_password: string; new_password: string }) =>
      adminPort.changePassword.write({ body }),
    invalidates: { screen: 'scr01', resources: ['sessions'] },
  });
  let issue: string | null = null;
  if (current === '') issue = 'scr01_admin.password.required';
  else if (Array.from(next).length < MIN_PASSWORD) issue = 'scr01_admin.password.short';
  else if (next !== confirm) issue = 'scr01_admin.password.mismatch';
  const errorId = `${formId}-error`;
  const edit = (set: (value: string) => void) => (value: string) => {
    mutation.reset();
    setChanged(false);
    set(value);
  };
  return (
    <form
      aria-label={text('scr01_admin.password.title')}
      aria-describedby={anchoredNoticeId(mutation.error, formId)}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        if (issue !== null) return;
        void mutation
          .run({ current_password: current, new_password: next })
          .then(() => {
            setCurrent('');
            setNext('');
            setConfirm('');
            setShowErrors(false);
            setChanged(true);
          })
          .catch(() => undefined);
      }}
    >
      <h2>{text('scr01_admin.password.title')}</h2>
      <WriteNotices error={mutation.error} formId={formId} />
      <label>
        {text('scr01_admin.password.current')}
        <input
          type="password"
          name="current_password"
          autoComplete="current-password"
          value={current}
          aria-invalid={showErrors && issue === 'scr01_admin.password.required'}
          onChange={(event) => {
            edit(setCurrent)(event.currentTarget.value);
          }}
        />
      </label>
      <label>
        {text('scr01_admin.password.new')}
        <input
          type="password"
          name="new_password"
          autoComplete="new-password"
          value={next}
          aria-invalid={showErrors && issue === 'scr01_admin.password.short'}
          onChange={(event) => {
            edit(setNext)(event.currentTarget.value);
          }}
        />
      </label>
      <label>
        {text('scr01_admin.password.confirm')}
        <input
          type="password"
          name="new_password_confirm"
          autoComplete="new-password"
          value={confirm}
          aria-invalid={showErrors && issue === 'scr01_admin.password.mismatch'}
          onChange={(event) => {
            edit(setConfirm)(event.currentTarget.value);
          }}
        />
      </label>
      {showErrors && issue !== null && (
        <p id={errorId} role="alert" data-text-key={issue}>
          {text(issue)}
        </p>
      )}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_admin.password.submit')}
      </button>
      {changed && (
        <p role="status" data-text-key="scr01_admin.password.changed">
          {text('scr01_admin.password.changed')}
        </p>
      )}
    </form>
  );
}

function Versions({ apiVersion }: { readonly apiVersion: string }) {
  const [release, setRelease] = useState<ReleaseInfo | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    readRelease(controller.signal)
      .then(setRelease)
      .catch(() => {
        if (!controller.signal.aborted) setRelease({ app_version: null, contract_tag: null });
      });
    return () => {
      controller.abort();
    };
  }, []);
  const unavailable = text('scr01_admin.versions.unavailable');
  const rows: readonly { key: string; value: string }[] = [
    { key: 'scr01_admin.versions.app', value: embeddedAppVersion() },
    { key: 'scr01_admin.versions.api', value: apiVersion },
    { key: 'scr01_admin.versions.contract', value: release?.contract_tag ?? unavailable },
  ];
  return (
    <section aria-label={text('scr01_admin.versions.title')}>
      <h2>{text('scr01_admin.versions.title')}</h2>
      <dl>
        {rows.map((row) => (
          <div key={row.key} data-text-key={row.key}>
            <dt>{text(row.key)}</dt>
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export default function Page() {
  const { principal } = useUiSession();
  const directory = useScopeDirectory();
  if (principal === null) {
    return (
      <main>
        <ScreenState state="loading" />
      </main>
    );
  }
  return (
    <main>
      <h1>{text('scr01_admin.account.title')}</h1>
      <AccountProfile account={principal} directory={directory} />
      <Sessions userId={principal.user_id} concession={principal.concession_id} />
      <PasswordForm />
      <Versions apiVersion={principal.api_version} />
    </main>
  );
}

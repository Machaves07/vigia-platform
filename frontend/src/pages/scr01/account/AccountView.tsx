/**
 * RV-01 (cuenta y sesiones): los componentes que pintan los datos de la cuenta y la lista de
 * sesiones, compartidos por la página y por la vista registrada para PR-APP-07. Solo nombre,
 * correo, organización, licencia, asignaciones con alcance y segundo factor; de cada sesión,
 * `client_hint`, `created_at`, `last_seen_at` y su estado. Una sesión no tiene identificador en
 * el cliente (`SessionView` no lo trae), así que ninguno se puede mostrar.
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { AccountSession } from '@/ports/scr01Admin';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { assignmentText, type ScopeDirectory } from '../scopes';

export interface AccountData {
  readonly display_name: string;
  readonly email: string;
  readonly organization: { readonly name: string };
  readonly professional_license?: string | null | undefined;
  readonly second_factor_enrolled?: boolean | undefined;
  readonly assignments: readonly {
    readonly role: string;
    readonly scope_level: string;
    readonly scope_id: string;
  }[];
}

export function AccountProfile({
  account,
  directory,
}: {
  readonly account: AccountData;
  readonly directory: ScopeDirectory;
}) {
  return (
    <section aria-label={text('scr01_admin.account.profile')}>
      <h2>{text('scr01_admin.account.profile')}</h2>
      <dl>
        <dt>{text('scr01_admin.account.name')}</dt>
        <dd>{account.display_name}</dd>
        <dt>{text('scr01_admin.account.email')}</dt>
        <dd>{account.email}</dd>
        <dt>{text('scr01_admin.account.organization')}</dt>
        <dd>{account.organization.name}</dd>
        <dt>{text('scr01_admin.account.license')}</dt>
        <dd>{account.professional_license ?? text('scr01_admin.account.no_license')}</dd>
        <dt>{text('scr01_admin.account.second_factor')}</dt>
        <dd>
          {account.second_factor_enrolled === true
            ? text('scr01_admin.account.second_factor_enrolled')
            : text('scr01_admin.account.second_factor_not_enrolled')}
        </dd>
        <dt>{text('scr01_admin.account.assignments')}</dt>
        <dd>
          <ul>
            {account.assignments.map((assignment, index) => (
              <li key={index}>{assignmentText(directory, assignment)}</li>
            ))}
          </ul>
        </dd>
      </dl>
    </section>
  );
}

function sessionState(session: AccountSession): string {
  if (session.current) return text('scr01_admin.sessions.current');
  return session.usable
    ? label('session_status', 'active')
    : text('scr01_admin.sessions.pending_second_factor');
}

export function SessionsList({
  sessions,
  timeZone,
}: {
  readonly sessions: readonly AccountSession[];
  readonly timeZone: string;
}) {
  return (
    <table>
      <caption>{text('scr01_admin.sessions.list')}</caption>
      <thead>
        <tr>
          <th scope="col">{text('scr01_admin.sessions.col_client')}</th>
          <th scope="col">{text('scr01_admin.sessions.col_created')}</th>
          <th scope="col">{text('scr01_admin.sessions.col_last_seen')}</th>
          <th scope="col">{text('scr01_admin.sessions.col_state')}</th>
        </tr>
      </thead>
      <tbody>
        {sessions.map((session, index) => (
          <tr key={index} data-current={session.current}>
            <td>{session.client_hint ?? text('scr01_admin.sessions.no_hint')}</td>
            <td>
              <TimestampWithOffset instant={session.created_at} timeZone={timeZone} />
            </td>
            <td>
              <TimestampWithOffset instant={session.last_seen_at} timeZone={timeZone} />
            </td>
            <td>{sessionState(session)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

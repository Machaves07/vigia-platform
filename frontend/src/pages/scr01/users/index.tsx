/**
 * SCR-01, grupo `users` (`users.manage`, `roles.manage`; RV-02; IF-02 paso 4): lista paginada por
 * clave de las cuentas de la organización con estado, licencia y asignaciones; invitación con el
 * enlace una sola vez mientras no exista el correo; gestión de cada cuenta (nombre, licencia,
 * roles, desactivar, reactivar y restablecer el segundo factor con motivo).
 *
 * La lista no pasa por la caché de consultas (la `KeysetTable` pide sus páginas): tras cada
 * escritura se vuelve a montar para pedir la primera página de nuevo.
 */
import { useState } from 'react';

import { KeysetTable } from '@/components/KeysetTable/KeysetTable';
import { adminClock, readUsers, type Invitation, type ManagedUser } from '@/ports/scr01Admin';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

import { useScopeDirectory } from '../scopes';
import { InvitationResult } from './InvitationResult';
import { InviteForm } from './InviteForm';
import { UserPanel } from './UserPanel';
import { userColumns } from './UsersView';

/** Zona horaria del navegador para los vencimientos de invitación (no son de una planta). */
function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

export default function Page() {
  const { principal } = useUiSession();
  const directory = useScopeDirectory();
  const [listRun, setListRun] = useState(0);
  const [inviting, setInviting] = useState(false);
  const [invitation, setInvitation] = useState<{ value: Invitation; key: number } | null>(null);
  const [selected, setSelected] = useState<ManagedUser | null>(null);
  const timeZone = browserTimeZone();
  const reload = () => {
    setListRun((run) => run + 1);
  };
  return (
    <main>
      <h1>{text('scr01_admin.users.title')}</h1>
      {invitation !== null && (
        <InvitationResult
          key={`invitation-${String(invitation.key)}`}
          invitation={invitation.value}
          timeZone={timeZone}
          onDone={() => {
            setInvitation(null);
          }}
        />
      )}
      {inviting ? (
        <InviteForm
          directory={directory}
          onInvited={(value) => {
            setInvitation((current) => ({ value, key: (current?.key ?? 0) + 1 }));
            setInviting(false);
            reload();
          }}
          onCancel={() => {
            setInviting(false);
          }}
        />
      ) : (
        <button
          type="button"
          onClick={() => {
            setInviting(true);
          }}
        >
          {text('scr01_admin.invite.open')}
        </button>
      )}
      <KeysetTable<ManagedUser>
        key={`list-${String(listRun)}`}
        caption={text('scr01_admin.users.list')}
        columns={userColumns(directory, setSelected)}
        rowKey={(row) => row.user_id}
        clock={adminClock()}
        emptyText={text('scr01_admin.users.empty')}
        fetchPage={(request, signal) => readUsers(request.cursor, request.pageSize, signal)}
      />
      {selected !== null && (
        <UserPanel
          key={`panel-${selected.user_id}`}
          user={selected}
          directory={directory}
          self={selected.user_id === principal?.user_id}
          timeZone={timeZone}
          onChanged={reload}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}
    </main>
  );
}

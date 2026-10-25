/**
 * Gestión de una cuenta (VIG-305): nombre y licencia (`PATCH /users/{user_id}`), roles con
 * alcance (asignar y retirar), desactivar y reactivar con confirmación, y restablecer el segundo
 * factor con motivo. Cada formulario tiene su propia escritura y su aviso anclado: un `conflict`
 * (último administrador, rol incompatible) se pinta con el mensaje del servidor en el formulario
 * que lo provocó y lo escrito en los demás se conserva. La pantalla no replica ninguna regla
 * (BR-APP-43): todas las acciones se ofrecen y el servidor decide.
 */
import { useId, useState } from 'react';

import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import {
  adminPort,
  RESET_REASON_LIMITS,
  type AssignmentBody,
  type Invitation,
  type ManagedUser,
  type UserAssignment,
} from '@/ports/scr01Admin';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { anchoredNoticeId, WriteNotices } from '../notices';
import { assignmentText, type ScopeDirectory } from '../scopes';
import {
  AssignmentList,
  AssignmentRow,
  completeAssignments,
  EMPTY_ASSIGNMENT,
  type AssignmentDraft,
} from './AssignmentFields';
import { InvitationResult } from './InvitationResult';

interface PanelContext {
  readonly user: ManagedUser;
  readonly directory: ScopeDirectory;
  /** La escritura cambia roles, licencia o estado del propio usuario: renueva `GET /me`. */
  readonly self: boolean;
  readonly timeZone: string;
  readonly onChanged: () => void;
}

function ProfileForm({ context }: { readonly context: PanelContext }) {
  const formId = useId();
  const { user } = context;
  const [saved, setSaved] = useState({
    name: user.display_name,
    license: user.professional_license ?? '',
  });
  const [name, setName] = useState(saved.name);
  const [license, setLicense] = useState(saved.license);
  const [clear, setClear] = useState(false);
  const [result, setResult] = useState<'saved' | 'unchanged' | null>(null);
  const mutation = useScreenMutation({
    write: (body: {
      display_name?: string;
      professional_license?: string;
      clear_professional_license: boolean;
    }) => adminPort.updateProfile.write({ path: { user_id: user.user_id }, body }),
    invalidates: { screen: 'scr01', resources: ['users'], affectsSelf: context.self },
  });
  return (
    <form
      aria-label={text('scr01_admin.profile.title')}
      aria-describedby={anchoredNoticeId(mutation.error, formId)}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        const trimmedName = name.trim();
        const trimmedLicense = license.trim();
        const body = {
          ...(trimmedName !== '' && trimmedName !== saved.name
            ? { display_name: trimmedName }
            : {}),
          ...(!clear && trimmedLicense !== '' && trimmedLicense !== saved.license
            ? { professional_license: trimmedLicense }
            : {}),
          clear_professional_license: clear,
        };
        if (body.display_name === undefined && body.professional_license === undefined && !clear) {
          setResult('unchanged');
          return;
        }
        void mutation
          .run(body)
          .then(() => {
            const next = {
              name: body.display_name ?? saved.name,
              license: clear ? '' : (body.professional_license ?? saved.license),
            };
            setSaved(next);
            setName(next.name);
            setLicense(next.license);
            setClear(false);
            setResult('saved');
            context.onChanged();
          })
          .catch(() => undefined);
      }}
    >
      <h3>{text('scr01_admin.profile.title')}</h3>
      <WriteNotices error={mutation.error} formId={formId} />
      <label>
        {text('scr01_admin.profile.name')}
        <input
          type="text"
          name="display_name"
          autoComplete="off"
          value={name}
          onChange={(event) => {
            mutation.reset();
            setResult(null);
            setName(event.currentTarget.value);
          }}
        />
      </label>
      <label>
        {text('scr01_admin.profile.license')}
        <input
          type="text"
          name="professional_license"
          autoComplete="off"
          value={license}
          disabled={clear}
          onChange={(event) => {
            mutation.reset();
            setResult(null);
            setLicense(event.currentTarget.value);
          }}
        />
      </label>
      <label>
        <input
          type="checkbox"
          name="clear_professional_license"
          checked={clear}
          onChange={(event) => {
            mutation.reset();
            setResult(null);
            setClear(event.currentTarget.checked);
          }}
        />
        {text('scr01_admin.profile.clear_license')}
      </label>
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_admin.profile.submit')}
      </button>
      {result !== null && (
        <p role="status" data-text-key={`scr01_admin.profile.${result}`}>
          {text(`scr01_admin.profile.${result}`)}
        </p>
      )}
    </form>
  );
}

function RolesForm({
  context,
  assignments,
  onAssignments,
}: {
  readonly context: PanelContext;
  readonly assignments: readonly UserAssignment[];
  readonly onAssignments: (assignments: UserAssignment[]) => void;
}) {
  const formId = useId();
  const { user } = context;
  const [draft, setDraft] = useState<AssignmentDraft>(EMPTY_ASSIGNMENT);
  const [showErrors, setShowErrors] = useState(false);
  const [result, setResult] = useState<'assigned' | 'removed' | null>(null);
  const invalidates = { screen: 'scr01', resources: ['users'], affectsSelf: context.self } as const;
  const assign = useScreenMutation({
    write: (body: AssignmentBody) =>
      adminPort.assignRole.write({ path: { user_id: user.user_id }, body }),
    invalidates,
  });
  const remove = useScreenMutation({
    write: (assignmentId: string) =>
      adminPort.removeRole.write({
        path: { user_id: user.user_id, assignment_id: assignmentId },
      }),
    invalidates,
  });
  const error = remove.error ?? assign.error;
  const busy = assign.status === 'pending' || remove.status === 'pending';
  const touch = () => {
    assign.reset();
    remove.reset();
    setResult(null);
  };
  return (
    <form
      aria-label={text('scr01_admin.roles.title')}
      aria-describedby={anchoredNoticeId(error, formId)}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        const body = completeAssignments([draft])?.[0];
        if (body === undefined) return;
        touch();
        void assign
          .run(body)
          .then((assigned) => {
            onAssignments([
              ...assignments,
              {
                assignment_id: assigned.assignment_id,
                role: assigned.role,
                scope_level: assigned.scope_level,
                scope_id: assigned.scope_id,
              },
            ]);
            setDraft(EMPTY_ASSIGNMENT);
            setShowErrors(false);
            setResult('assigned');
            context.onChanged();
          })
          .catch(() => undefined);
      }}
    >
      <h3>{text('scr01_admin.roles.title')}</h3>
      <WriteNotices error={error} formId={formId} />
      <h4>{text('scr01_admin.roles.current')}</h4>
      {assignments.length === 0 ? (
        <p>{text('scr01_admin.users.no_assignments')}</p>
      ) : (
        <ul>
          {assignments.map((assignment) => {
            const caption = assignmentText(context.directory, assignment);
            return (
              <li key={assignment.assignment_id}>
                {caption}{' '}
                <button
                  type="button"
                  aria-label={`${text('scr01_admin.roles.remove')}: ${caption}`}
                  disabled={busy}
                  onClick={() => {
                    touch();
                    void remove
                      .run(assignment.assignment_id)
                      .then(() => {
                        onAssignments(
                          assignments.filter(
                            (item) => item.assignment_id !== assignment.assignment_id,
                          ),
                        );
                        setResult('removed');
                        context.onChanged();
                      })
                      .catch(() => undefined);
                  }}
                >
                  {text('scr01_admin.roles.remove')}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <fieldset>
        <legend>{text('scr01_admin.roles.add_title')}</legend>
        <AssignmentRow
          draft={draft}
          directory={context.directory}
          invalid={showErrors}
          onChange={(changed) => {
            assign.reset();
            setResult(null);
            setDraft(changed);
          }}
        />
      </fieldset>
      {showErrors && completeAssignments([draft]) === null && (
        <p role="alert" data-text-key="scr01_admin.assignment.incomplete">
          {text('scr01_admin.assignment.incomplete')}
        </p>
      )}
      <button type="submit" disabled={busy}>
        {text('scr01_admin.roles.add')}
      </button>
      {result !== null && (
        <p role="status" data-text-key={`scr01_admin.roles.${result}`}>
          {text(`scr01_admin.roles.${result}`)}
        </p>
      )}
    </form>
  );
}

function StatusForm({
  context,
  status,
  onStatus,
}: {
  readonly context: PanelContext;
  readonly status: string;
  readonly onStatus: (status: string) => void;
}) {
  const formId = useId();
  const { user } = context;
  const [confirming, setConfirming] = useState(false);
  const [drafts, setDrafts] = useState<AssignmentDraft[]>([EMPTY_ASSIGNMENT]);
  const [showErrors, setShowErrors] = useState(false);
  const [done, setDone] = useState(false);
  const [invitation, setInvitation] = useState<{ value: Invitation; key: number } | null>(null);
  const invalidates = { screen: 'scr01', resources: ['users'], affectsSelf: context.self } as const;
  const deactivate = useScreenMutation({
    write: () => adminPort.deactivate.write({ path: { user_id: user.user_id } }),
    invalidates,
  });
  const reactivate = useScreenMutation({
    // El enlace no entra en la caché de mutaciones: solo en el estado de este formulario.
    write: async (assignments: AssignmentBody[]) => {
      const result = await adminPort.reactivate.write({
        path: { user_id: user.user_id },
        body: { assignments, disclose_link: true },
      });
      setInvitation((current) => ({ value: result, key: (current?.key ?? 0) + 1 }));
      return { delivery: result.delivery };
    },
    invalidates,
  });
  const deactivated = status === 'deactivated';
  const mutation = deactivated ? reactivate : deactivate;
  return (
    <form
      aria-label={text('scr01_admin.status.title')}
      aria-describedby={anchoredNoticeId(mutation.error, formId)}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (!deactivated) {
          void deactivate
            .run(undefined)
            .then(() => {
              setConfirming(false);
              setDone(true);
              onStatus('deactivated');
              context.onChanged();
            })
            .catch(() => undefined);
          return;
        }
        setShowErrors(true);
        const bodies = completeAssignments(drafts);
        if (bodies === null) return;
        void reactivate
          .run(bodies)
          .then(() => {
            setConfirming(false);
            setDrafts([EMPTY_ASSIGNMENT]);
            setShowErrors(false);
            onStatus('invited');
            context.onChanged();
          })
          .catch(() => undefined);
      }}
    >
      <h3>{text('scr01_admin.status.title')}</h3>
      <p>{label('account_status', status)}</p>
      <WriteNotices error={mutation.error} formId={formId} />
      {done && deactivated && (
        <p role="status" data-text-key="scr01_admin.status.deactivated">
          {text('scr01_admin.status.deactivated')}
        </p>
      )}
      {invitation !== null && (
        <InvitationResult
          key={invitation.key}
          invitation={invitation.value}
          timeZone={context.timeZone}
          onDone={() => {
            setInvitation(null);
          }}
        />
      )}
      {!confirming && (
        <button
          type="button"
          onClick={() => {
            mutation.reset();
            setDone(false);
            setConfirming(true);
          }}
        >
          {deactivated
            ? text('scr01_admin.status.reactivate')
            : text('scr01_admin.status.deactivate')}
        </button>
      )}
      {confirming && !deactivated && (
        <>
          <p>{text('scr01_admin.status.deactivate_confirm')}</p>
          <button type="submit" disabled={deactivate.status === 'pending'}>
            {text('scr01_admin.status.confirm')}
          </button>
        </>
      )}
      {confirming && deactivated && (
        <>
          <p>{text('scr01_admin.status.reactivate_intro')}</p>
          <AssignmentList
            drafts={drafts}
            directory={context.directory}
            invalid={showErrors}
            onChange={(changed) => {
              reactivate.reset();
              setDrafts(changed);
            }}
          />
          <button type="submit" disabled={reactivate.status === 'pending'}>
            {text('scr01_admin.status.reactivate_confirm')}
          </button>
        </>
      )}
      {confirming && (
        <button
          type="button"
          onClick={() => {
            mutation.reset();
            setConfirming(false);
          }}
        >
          {text('scr01_admin.status.cancel')}
        </button>
      )}
    </form>
  );
}

/**
 * Restablecimiento del segundo factor con motivo obligatorio (IF-02 paso 4). La ruta de U-02 no
 * tiene cuerpo: el motivo se exige antes de enviar y no viaja (ver el PR de VIG-305).
 */
function ResetForm({ context }: { readonly context: PanelContext }) {
  const formId = useId();
  const { user } = context;
  const [reason, setReason] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const [closed, setClosed] = useState<number | null>(null);
  const mutation = useScreenMutation({
    write: () => adminPort.resetSecondFactor.write({ path: { user_id: user.user_id } }),
    invalidates: { screen: 'scr01', resources: ['users', 'sessions'], affectsSelf: context.self },
  });
  return (
    <form
      aria-label={text('scr01_admin.reset.title')}
      aria-describedby={anchoredNoticeId(mutation.error, formId)}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        if (reasonIssueKey(reason, RESET_REASON_LIMITS) !== null) return;
        setClosed(null);
        void mutation
          .run(undefined)
          .then((result) => {
            setReason('');
            setShowErrors(false);
            setClosed(result.sessions_closed);
            context.onChanged();
          })
          .catch(() => undefined);
      }}
    >
      <h3>{text('scr01_admin.reset.title')}</h3>
      <WriteNotices error={mutation.error} formId={formId} />
      <ReasonField
        label={text('scr01_admin.reset.reason')}
        name="reason"
        value={reason}
        limits={RESET_REASON_LIMITS}
        showErrors={showErrors}
        onChange={(value) => {
          mutation.reset();
          setClosed(null);
          setReason(value);
        }}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_admin.reset.submit')}
      </button>
      {closed !== null && (
        <div role="status" data-text-key="scr01_admin.reset.done">
          <p data-part="sessions_closed">{`${text('scr01_admin.reset.done')} ${String(closed)}`}</p>
          <p>{text('scr01_admin.reset.enroll_next')}</p>
        </div>
      )}
    </form>
  );
}

export function UserPanel({
  user,
  directory,
  self,
  timeZone,
  onChanged,
  onClose,
}: PanelContext & { readonly onClose: () => void }) {
  const [assignments, setAssignments] = useState<UserAssignment[]>([...user.assignments]);
  const [status, setStatus] = useState(user.status);
  const context: PanelContext = { user, directory, self, timeZone, onChanged };
  const title = `${text('scr01_admin.panel.title')} ${user.display_name}`;
  return (
    <section aria-label={title}>
      <h2>{title}</h2>
      <button type="button" onClick={onClose}>
        {text('scr01_admin.panel.close')}
      </button>
      <ProfileForm context={context} />
      <RolesForm context={context} assignments={assignments} onAssignments={setAssignments} />
      <StatusForm context={context} status={status} onStatus={setStatus} />
      <ResetForm context={context} />
    </section>
  );
}

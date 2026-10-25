/**
 * Invitación (`POST /users`, BR-NUC-30 y 32): correo, nombre, licencia opcional y al menos una
 * asignación. Mientras no exista el correo de invitación (`assumed` de U-04) se pide siempre
 * `disclose_link`, y el enlace se muestra **una vez** en `CopyOnceSecret`.
 *
 * El enlace nunca entra en la caché de mutaciones: la escritura lo entrega por `onInvited` al
 * estado de la página y la mutación solo guarda la forma de entrega. Al salir de la pantalla o al
 * confirmar «lo guardé», el enlace desaparece.
 */
import { useId, useState } from 'react';

import { adminPort, type Invitation } from '@/ports/scr01Admin';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';

import { anchoredNoticeId, WriteNotices } from '../notices';
import type { ScopeDirectory } from '../scopes';
import {
  AssignmentList,
  completeAssignments,
  EMPTY_ASSIGNMENT,
  type AssignmentDraft,
} from './AssignmentFields';

/** Estructura del correo (BR-APP-42); el servidor decide si se puede invitar. */
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/u;

export function InviteForm({
  directory,
  onInvited,
  onCancel,
}: {
  readonly directory: ScopeDirectory;
  readonly onInvited: (invitation: Invitation) => void;
  readonly onCancel: () => void;
}) {
  const formId = useId();
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [license, setLicense] = useState('');
  const [assignments, setAssignments] = useState<AssignmentDraft[]>([EMPTY_ASSIGNMENT]);
  const [showErrors, setShowErrors] = useState(false);
  const mutation = useScreenMutation({
    write: async (body: {
      email: string;
      display_name: string;
      professional_license?: string;
      assignments: NonNullable<ReturnType<typeof completeAssignments>>;
    }) => {
      const invitation = await adminPort.invite.write({ body: { ...body, disclose_link: true } });
      onInvited(invitation);
      return { delivery: invitation.delivery };
    },
    invalidates: { screen: 'scr01', resources: ['users'] },
  });
  const emailInvalid = !EMAIL.test(email.trim());
  const nameMissing = name.trim() === '';
  const edit =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      mutation.reset();
      set(value);
    };
  return (
    <form
      aria-label={text('scr01_admin.invite.title')}
      aria-describedby={anchoredNoticeId(mutation.error, formId)}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setShowErrors(true);
        const bodies = completeAssignments(assignments);
        if (emailInvalid || nameMissing || bodies === null) return;
        const trimmed = license.trim();
        void mutation
          .run({
            email: email.trim(),
            display_name: name.trim(),
            ...(trimmed === '' ? {} : { professional_license: trimmed }),
            assignments: bodies,
          })
          .catch(() => undefined);
      }}
    >
      <h2>{text('scr01_admin.invite.title')}</h2>
      <WriteNotices error={mutation.error} formId={formId} />
      <label>
        {text('scr01_admin.invite.email')}
        <input
          type="email"
          name="email"
          autoComplete="off"
          value={email}
          aria-invalid={showErrors && emailInvalid}
          onChange={(event) => {
            edit(setEmail)(event.currentTarget.value);
          }}
        />
      </label>
      {showErrors && emailInvalid && (
        <p role="alert" data-text-key="scr01_admin.invite.email_invalid">
          {text('scr01_admin.invite.email_invalid')}
        </p>
      )}
      <label>
        {text('scr01_admin.invite.name')}
        <input
          type="text"
          name="display_name"
          autoComplete="off"
          value={name}
          aria-invalid={showErrors && nameMissing}
          onChange={(event) => {
            edit(setName)(event.currentTarget.value);
          }}
        />
      </label>
      {showErrors && nameMissing && (
        <p role="alert" data-text-key="scr01_admin.invite.name_required">
          {text('scr01_admin.invite.name_required')}
        </p>
      )}
      <label>
        {text('scr01_admin.invite.license')}
        <input
          type="text"
          name="professional_license"
          autoComplete="off"
          value={license}
          onChange={(event) => {
            edit(setLicense)(event.currentTarget.value);
          }}
        />
      </label>
      <AssignmentList
        drafts={assignments}
        directory={directory}
        invalid={showErrors}
        onChange={edit(setAssignments)}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_admin.invite.submit')}
      </button>
      <button type="button" onClick={onCancel}>
        {text('scr01_admin.invite.cancel')}
      </button>
    </form>
  );
}

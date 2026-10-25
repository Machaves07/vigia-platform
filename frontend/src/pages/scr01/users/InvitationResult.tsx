/**
 * Resultado de una invitación o reactivación: el enlace una sola vez (`link_disclosed`, UI-24) o
 * el aviso de que salió por correo (`email_queued`). Montarlo con otra `key` muestra otro enlace.
 */
import { CopyOnceSecret } from '@/components/CopyOnceSecret/CopyOnceSecret';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { Invitation } from '@/ports/scr01Admin';
import { text } from '@/texts';

export function InvitationResult({
  invitation,
  timeZone,
  onDone,
}: {
  readonly invitation: Invitation;
  readonly timeZone: string;
  readonly onDone: () => void;
}) {
  return (
    <section aria-live="polite">
      {invitation.link === null ? (
        <>
          <p role="status" data-text-key="scr01_admin.invite.email_queued">
            {text('scr01_admin.invite.email_queued')}
          </p>
          <button type="button" onClick={onDone}>
            {text('scr01_admin.invite.done')}
          </button>
        </>
      ) : (
        <>
          <p>{text('scr01_admin.invite.link_once')}</p>
          <CopyOnceSecret kind="invitation_link" value={invitation.link} onAcknowledged={onDone} />
        </>
      )}
      <p>
        {text('scr01_admin.invite.expires')}{' '}
        <TimestampWithOffset instant={invitation.expires_at} timeZone={timeZone} />
      </p>
    </section>
  );
}

/**
 * Sesión incompleta (IF-13 paso 1; respuesta 12 = C de U-03): una sesión sin actividad durante 7
 * días queda incompleta y solo continúa si alguien la reabre con motivo. La pantalla no decide el
 * plazo: muestra lo que el servidor marcó (`status = incomplete`) y envía el motivo.
 */
import { useId, useState } from 'react';

import type { Notice } from '@/components/NoticeStack/notice';
import { ReasonInput } from '@/pages/scr06/common/ReasonInput';
import { fill } from '@/pages/scr06/common/format';
import type { WalkTestSession } from '@/ports/scr06Commissioning';
import { text } from '@/texts';

import styles from './session.module.css';

export interface ReopenPanelProps {
  readonly session: WalkTestSession;
  readonly onReopen: (reasonEs: string) => void;
  readonly busy?: boolean;
  readonly notice?: Notice | undefined;
  readonly errorKey?: string | null;
}

export function ReopenPanel({
  session,
  onReopen,
  busy = false,
  notice,
  errorKey = null,
}: ReopenPanelProps) {
  const [reason, setReason] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const headingId = useId();
  if (session.status !== 'incomplete') return null;
  return (
    <section className={styles.incomplete} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.subtitle}>
        {text('scr06.session.incomplete.title')}
      </h3>
      <p data-part="incomplete_since">
        {fill(text('scr06.session.incomplete.since'), { since: session.last_activity_at })}
      </p>
      <p className={styles.hint}>{text('scr06.session.incomplete.body')}</p>
      <form
        className={styles.form}
        aria-label={text('scr06.session.reopen.submit')}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (reason.trim() === '') {
            setShowErrors(true);
            return;
          }
          onReopen(reason);
        }}
      >
        <ReasonInput
          label={text('scr06.session.reopen.reason')}
          value={reason}
          onChange={setReason}
          name="reopen_reason_es"
          notice={notice}
          errorKey={errorKey}
          showErrors={showErrors}
        />
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={busy} data-part="reopen">
            {text(busy ? 'scr06.session.reopen.submitting' : 'scr06.session.reopen.submit')}
          </button>
        </div>
      </form>
    </section>
  );
}

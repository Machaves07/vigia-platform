/**
 * UI-08 `PrivacyNoticeGate` (IF-03, BR-APP-13, NFR-APP-33): con `privacy_notice_pending` ocupa
 * toda la pantalla y solo deja dos acciones: «Acepto» (`POST /privacy-notice/accept` con la
 * versión mostrada) y «Cerrar sesión». Tras aceptar renueva el `Principal` (`GET /me`) y vuelve a
 * la ruta guardada. Ni enlaces ni navegación: nada más es alcanzable hasta aceptar.
 */
import { useId, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';

import { sessionFailure, sessionScreenPort, type SessionFailure } from '@/ports/scr01Session';
import { useSession, useUiSession } from '@/state/session';
import { text } from '@/texts';

import { NoticeBody, noticeText, noticeVersion } from './privacyNotice';
import styles from './scr01.module.css';
import { FailureNotice, returnRoute } from './session';

export function PrivacyNoticeGate(): ReactNode {
  const session = useSession();
  const { principal } = useUiSession();
  const navigate = useNavigate();
  const id = useId();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<SessionFailure | null>(null);

  const version = noticeVersion(principal?.privacy_notice_current_version);
  const body = version === null ? null : noticeText(version);

  const accept = async () => {
    if (version === null || body === null || pending) return;
    setPending(true);
    setFailure(null);
    try {
      await sessionScreenPort.acceptNotice.write({ body: { notice_version: version } });
      await session.load();
      await navigate(returnRoute(session), { replace: true });
    } catch (error) {
      setFailure(sessionFailure(error));
    } finally {
      setPending(false);
    }
  };

  return (
    <main className={styles.page} aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} className={styles.title}>
        {text('scr01.privacy.title')}
      </h1>
      <p>{text('scr01.privacy.intro')}</p>
      {version === null ? null : (
        <p data-part="version">
          {text('scr01.privacy.version')}: <strong>{version}</strong>
        </p>
      )}
      {body === null ? (
        <p className={styles.error} role="alert" data-text-key="scr01.privacy.text_unavailable">
          {text('scr01.privacy.text_unavailable')}
        </p>
      ) : (
        <section className={styles.notice} aria-labelledby={`${id}-title`}>
          <NoticeBody body={body} />
        </section>
      )}
      <FailureNotice failure={failure} invalidKey="scr01.privacy.rejected" remaining={0} />
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          disabled={body === null || pending}
          onClick={() => {
            void accept();
          }}
        >
          {text('scr01.privacy.accept')}
        </button>
        <button
          type="button"
          className={styles.button}
          onClick={() => {
            void session.logout();
          }}
        >
          {text('scr01.privacy.logout')}
        </button>
      </div>
    </main>
  );
}

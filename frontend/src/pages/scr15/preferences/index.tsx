/** GET y PUT propios: sin identificador de usuario ni preferencia para el nivel inmediato. */
import { Link } from 'react-router';
import { notificationsPort } from '@/ports/scr15Notifications';
import { text } from '@/texts';
import { NotificationsResult, useNotificationsContext, useNotificationsRead } from '../shared';
import { PreferencesForm } from './PreferencesForm';
import styles from '../scr15.module.css';

export default function Page() {
  const context = useNotificationsContext();
  const query = useNotificationsRead('GET /notifications/preferences', {}, (signal) =>
    notificationsPort.preferences.read({}, signal),
  );
  return (
    <main className={styles.page}>
      <h1>{text('scr15.preferences')}</h1>
      <Link to="/notifications">{text('scr15.title')}</Link>
      <NotificationsResult query={query}>
        {query.data !== undefined && (
          <PreferencesForm key={`${context.scope}:${context.concession ?? ''}`} data={query.data} />
        )}
      </NotificationsResult>
    </main>
  );
}

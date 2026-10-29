/** RV-19: campos expresos; nunca cuerpos, imágenes ni objetos abiertos del aviso. */
import { Link } from 'react-router';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { Notification } from '@/ports/scr15Notifications';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import styles from './scr15.module.css';

/** Solo destinos internos conocidos; un kind nuevo no se interpreta como URL. */
export function notificationDestination(item: Notification): string | null {
  const id = encodeURIComponent(item.deep_link_ref.id);
  switch (item.deep_link_ref.kind) {
    case 'finding':
      return item.event_name === 'action_assigned' || item.event_name === 'action_evidence_added'
        ? `/findings/${id}/loop`
        : `/findings/${id}`;
    case 'queue_item':
      return `/review-queue?item_id=${id}`;
    // U-04 solo facilita package_id, no zona y periodo: se abre el selector mensual.
    case 'package':
      return `/zones/monthly?package_id=${id}`;
    case 'export':
      return `/exports?export_id=${id}`;
    case 'zone':
      // No hay destino común a todos los destinatarios; SCR-16 exige otro permiso.
      return null;
    case 'node':
      return `/fleet/nodes/${id}`;
    case 'organization':
      return '/admin/organization';
    default:
      return null;
  }
}

/**
 * Un aviso en pocas líneas (VIG-366): sin la fila «Zona no facilitada» (el aviso no la trae) y la
 * hora en la zona de la planta del alcance, no en UTC.
 */
export function NotificationView({
  item,
  timeZone,
}: {
  readonly item: Notification;
  readonly timeZone: string;
}) {
  const destination = notificationDestination(item);
  return (
    <dl className={styles.facts}>
      <dt>{text('scr15.type')}</dt>
      <dd>{item.title_es}</dd>
      <dt>{text('scr15.level')}</dt>
      <dd>{label('notification_urgency', item.urgency)}</dd>
      <dt>{text('scr15.moment')}</dt>
      <dd>
        <TimestampWithOffset instant={item.created_at} timeZone={timeZone} />
      </dd>
      <dt>{text('scr15.status')}</dt>
      <dd>{text(item.read_at === null ? 'scr15.unread' : 'scr15.read')}</dd>
      <dt>{text('scr15.origin')}</dt>
      <dd>
        {destination === null ? (
          text('scr15.origin_unavailable')
        ) : (
          <Link to={destination}>{text('scr15.open_origin')}</Link>
        )}
      </dd>
    </dl>
  );
}

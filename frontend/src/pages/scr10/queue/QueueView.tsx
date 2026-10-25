/** RV-14: campos permitidos de la proyección; nunca se vuelca un objeto del servidor. */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { Plant, QueueItem } from '@/ports/scr10ReviewQueue';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import styles from './queue.module.css';

export function QueueView({
  items,
  plants,
  now,
  canResolve,
  onResolve,
  onEvidence,
}: {
  readonly items: readonly QueueItem[];
  readonly plants: readonly Plant[];
  readonly now: number;
  readonly canResolve: boolean;
  readonly onResolve: (item: QueueItem) => void;
  readonly onEvidence: (item: QueueItem) => void;
}) {
  return (
    <table className={styles.table}>
      <caption>{text('scr10.title')}</caption>
      <thead>
        <tr>
          {['zone', 'family', 'confidence', 'due', 'review_reason', 'reminders', 'actions'].map(
            (field) => (
              <th scope="col" key={field}>
                {text(`scr10.${field}`)}
              </th>
            ),
          )}
        </tr>
      </thead>
      <tbody>
        {items.map((item) => {
          const plant = plants.find((p) => p.plant_id === item.plant_id);
          const overdue = Date.parse(item.review_due_at) <= now;
          return (
            <tr
              key={item.detection_id}
              data-overdue={overdue}
              className={overdue ? styles.overdue : undefined}
            >
              <th scope="row">
                {plant?.zones.find((z) => z.zone_id === item.zone_id)?.name ??
                  text('common.unknown')}
              </th>
              <td>{label('predicate_family', item.family)}</td>
              <td>
                <dl className={styles.context}>
                  {(['max_confidence', 'review_threshold', 'publication_threshold'] as const).map(
                    (field) => (
                      <div key={field}>
                        <dt>
                          {text(field === 'max_confidence' ? 'scr10.confidence' : `scr10.${field}`)}
                        </dt>
                        <dd data-field={field}>{item[field]}</dd>
                      </div>
                    ),
                  )}
                </dl>
              </td>
              <td>
                {overdue && <strong>{text('scr10.overdue')}</strong>}
                <TimestampWithOffset
                  instant={item.review_due_at}
                  timeZone={plant?.timezone ?? ''}
                  {...(plant === undefined ? {} : { plantName: plant.name })}
                />
              </td>
              <td>
                {['low_confidence', 'ambiguous', 'no_bounding_box', 'other'].some(
                  (value) => value === item.review_reason,
                )
                  ? text(`scr10.review_reason.${item.review_reason}`)
                  : text('common.unknown')}
              </td>
              <td>{item.reminders_sent}</td>
              <td>
                <button
                  type="button"
                  onClick={() => {
                    onEvidence(item);
                  }}
                >
                  {text('scr10.evidence')}
                </button>
                {canResolve && (
                  <button
                    type="button"
                    onClick={() => {
                      onResolve(item);
                    }}
                  >
                    {text('scr10.resolve')}
                  </button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

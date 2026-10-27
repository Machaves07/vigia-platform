/**
 * RV-14: campos permitidos de la proyección; nunca se vuelca un objeto del servidor.
 *
 * VIG-359: el panel de evidencia o de resolución de una detección se abre en la fila siguiente a
 * la suya, no al final de la página; cada botón declara si su panel está abierto y cuál es
 * (`aria-expanded`, `aria-controls`). La detección señalada desde el hallazgo (`target`) se marca
 * como la actual (`aria-current`) y su cabecera de fila puede recibir el foco.
 */
import type { ReactNode, Ref } from 'react';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { Plant, QueueItem } from '@/ports/scr10ReviewQueue';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import styles from './queue.module.css';

const COLUMNS = ['zone', 'family', 'confidence', 'due', 'review_reason', 'reminders', 'actions'];

export type PanelKind = 'resolve' | 'evidence';

export interface OpenPanel {
  readonly detectionId: string;
  readonly kind: PanelKind;
}

/** Identificador del panel de una detección (para `aria-controls`). */
export function panelId(detectionId: string): string {
  return `review-panel-${detectionId}`;
}

export function QueueView({
  items,
  plants,
  now,
  canResolve,
  onResolve,
  onEvidence,
  open = null,
  panel = null,
  target = null,
  targetRef,
}: {
  readonly items: readonly QueueItem[];
  readonly plants: readonly Plant[];
  readonly now: number;
  readonly canResolve: boolean;
  readonly onResolve: (item: QueueItem, trigger: HTMLButtonElement) => void;
  readonly onEvidence: (item: QueueItem, trigger: HTMLButtonElement) => void;
  /** El panel abierto, si lo hay. */
  readonly open?: OpenPanel | null;
  /** El contenido del panel abierto: se pinta en la fila siguiente a la de su detección. */
  readonly panel?: ReactNode;
  /** La detección señalada desde el detalle del hallazgo. */
  readonly target?: string | null;
  readonly targetRef?: Ref<HTMLTableCellElement>;
}) {
  return (
    <table className={styles.table}>
      <caption>{text('scr10.title')}</caption>
      <thead>
        <tr>
          {COLUMNS.map((field) => (
            <th scope="col" key={field}>
              {text(`scr10.${field}`)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((item) => {
          const plant = plants.find((p) => p.plant_id === item.plant_id);
          const overdue = Date.parse(item.review_due_at) <= now;
          const isTarget = item.detection_id === target;
          const opened = open?.detectionId === item.detection_id ? open.kind : null;
          const zone =
            plant?.zones.find((z) => z.zone_id === item.zone_id)?.name ?? text('common.unknown');
          return [
            <tr
              key={item.detection_id}
              data-overdue={overdue}
              data-target={isTarget || undefined}
              aria-current={isTarget || undefined}
              className={
                [overdue ? styles.overdue : '', isTarget ? styles.target : '']
                  .filter(Boolean)
                  .join(' ') || undefined
              }
            >
              <th scope="row" {...(isTarget ? { ref: targetRef, tabIndex: -1 } : {})}>
                {zone}
                {isTarget && <p data-part="target">{text('finding_journey.queue_target')}</p>}
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
                  aria-expanded={opened === 'evidence'}
                  aria-controls={opened === 'evidence' ? panelId(item.detection_id) : undefined}
                  onClick={(event) => {
                    onEvidence(item, event.currentTarget);
                  }}
                >
                  {text('scr10.evidence')}
                </button>
                {canResolve && (
                  <button
                    type="button"
                    aria-expanded={opened === 'resolve'}
                    aria-controls={opened === 'resolve' ? panelId(item.detection_id) : undefined}
                    onClick={(event) => {
                      onResolve(item, event.currentTarget);
                    }}
                  >
                    {text('scr10.resolve')}
                  </button>
                )}
              </td>
            </tr>,
            opened !== null && (
              <tr key={`${item.detection_id}:panel`} data-part="panel_row">
                <td colSpan={COLUMNS.length}>{panel}</td>
              </tr>
            ),
          ];
        })}
      </tbody>
    </table>
  );
}

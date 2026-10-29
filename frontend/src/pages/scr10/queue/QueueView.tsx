/**
 * RV-14: campos permitidos de la proyección; nunca se vuelca un objeto del servidor.
 *
 * VIG-359: el panel de evidencia o de resolución de una detección se abre en la fila siguiente a
 * la suya, no al final de la página; cada botón declara si su panel está abierto y cuál es
 * (`aria-expanded`, `aria-controls`). La detección señalada desde el hallazgo (`target`) se marca
 * como la actual (`aria-current`) y su cabecera de fila puede recibir el foco.
 */
import type { ReactNode, Ref } from 'react';
import {
  TimestampWithOffset,
  ZoneHeader,
} from '@/components/TimestampWithOffset/TimestampWithOffset';
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
  // La zona horaria una vez en la cabecera si todas las filas son de la misma (VIG-366).
  const zones = new Set(
    items.map((item) => plants.find((p) => p.plant_id === item.plant_id)?.timezone ?? ''),
  );
  const tableZone = zones.size === 1 ? ([...zones][0] ?? '') : '';
  const zoneMode = tableZone === '' ? 'inline' : 'header';
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption>{text('scr10.title')}</caption>
        <thead>
          <tr>
            {COLUMNS.map((field) => (
              <th scope="col" key={field}>
                {text(field === 'confidence' ? 'scr10.confidence_short' : `scr10.${field}`)}
                {field === 'due' && tableZone !== '' && <ZoneHeader timeZone={tableZone} />}
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
                {/* Solo la confianza; los umbrales van una vez, en `QueueThresholds` (VIG-366). */}
                <td>
                  <span className={styles.confidence} data-field="max_confidence">
                    {item.max_confidence}
                  </span>
                </td>
                <td>
                  {overdue && <strong>{text('scr10.overdue')} </strong>}
                  <TimestampWithOffset
                    instant={item.review_due_at}
                    timeZone={plant?.timezone ?? ''}
                    zone={zoneMode}
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
    </div>
  );
}

/**
 * Los umbrales de contexto una sola vez (VIG-366; antes se repetían en cada fila): si todas las
 * detecciones comparten umbrales, una línea; si no, una por zona.
 */
export function QueueThresholds({
  items,
  plants,
}: {
  readonly items: readonly QueueItem[];
  readonly plants: readonly Plant[];
}) {
  const byZone = new Map<string, { review: number; publication: number }>();
  for (const item of items) {
    byZone.set(item.zone_id, {
      review: item.review_threshold,
      publication: item.publication_threshold,
    });
  }
  const distinct = new Set(
    [...byZone.values()].map((t) => `${String(t.review)}:${String(t.publication)}`),
  );
  if (byZone.size === 0) return null;
  const line = (t: { review: number; publication: number }) =>
    `${text('scr10.review_threshold')}: ${String(t.review)} · ${text('scr10.publication_threshold')}: ${String(t.publication)}`;
  const zoneName = (zoneId: string) =>
    plants.flatMap((p) => p.zones).find((z) => z.zone_id === zoneId)?.name ??
    text('common.unknown');
  return (
    <section
      className={styles.thresholds}
      aria-label={text('scr10.thresholds')}
      data-part="thresholds"
    >
      <h2>{text('scr10.thresholds')}</h2>
      {distinct.size === 1 ? (
        <p>{line([...byZone.values()][0] ?? { review: 0, publication: 0 })}</p>
      ) : (
        <ul>
          {[...byZone.entries()].map(([zoneId, t]) => (
            <li key={zoneId}>
              {zoneName(zoneId)}: {line(t)}
            </li>
          ))}
        </ul>
      )}
      <p>{text('scr10.confidence_context')}</p>
    </section>
  );
}

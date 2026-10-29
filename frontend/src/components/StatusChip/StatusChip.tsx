/**
 * Chip de estado del hallazgo (sistema visual v1, «Componentes»): el mismo en el inicio, la
 * bandeja, el detalle y el lazo. Icono y texto siempre; el color (`--state-*`) solo refuerza y
 * habla del plazo de una acción, nunca de la zona (P2). El texto dice el tiempo cuando el servidor
 * lo da: «Vencida hace 2 días» con `overdue_days` y «Vence dentro de 3 días» con `due_at`. El
 * vencimiento lo declara el servidor (`overdue`); el chip nunca lo deduce de la fecha.
 */
import { text } from '@/texts';

import styles from './StatusChip.module.css';

/** Tono de cada estado de `finding_lifecycle_state`; fuera de la lista, neutro. */
const TONES: Readonly<Record<string, string>> = {
  received: 'neutral',
  in_review: 'neutral',
  discarded: 'neutral',
  suspended_by_reclassification: 'neutral',
  open: 'open',
  action_assigned: 'open',
  overdue: 'overdue',
  closed: 'closed',
  unclosable: 'unclosable',
};

/** Días que se consideran «esta semana» para el plazo de una acción asignada. */
export const DUE_SOON_DAYS = 7;

const DAY_MS = 86_400_000;

const relative = new Intl.RelativeTimeFormat('es-CO', { numeric: 'always' });

/** Días enteros que faltan hasta `dueAt` desde `now` (0 si vence hoy o ya pasó la hora). */
export function daysUntil(dueAt: string, now: number): number | null {
  const due = Date.parse(dueAt);
  if (!Number.isFinite(due)) return null;
  return Math.max(0, Math.ceil((due - now) / DAY_MS));
}

function known(state: string): boolean {
  return Object.hasOwn(TONES, state);
}

/** El texto del chip: el del estado y, si se conoce, su tiempo. */
function statusText(
  state: string,
  timing: { readonly overdueDays?: number; readonly dueAt?: string | null; readonly now?: number },
): string {
  if (!known(state)) return text('status_chip.unknown');
  if (state === 'overdue' && timing.overdueDays !== undefined && timing.overdueDays > 0) {
    return `${text('status_chip.overdue_since')} ${relative.format(-timing.overdueDays, 'day')}`;
  }
  if (
    state === 'action_assigned' &&
    timing.dueAt !== undefined &&
    timing.dueAt !== null &&
    timing.now !== undefined
  ) {
    const days = daysUntil(timing.dueAt, timing.now);
    if (days === 0) return text('status_chip.due_today');
    if (days !== null) return `${text('status_chip.due_in')} ${relative.format(days, 'day')}`;
  }
  return text(`status_chip.${state}`);
}

/** El tono: una acción asignada que vence esta semana se marca como «vence pronto». */
function statusTone(state: string, dueAt: string | null | undefined, now?: number): string {
  if (!known(state)) return 'neutral';
  if (state === 'action_assigned' && dueAt != null && now !== undefined) {
    const days = daysUntil(dueAt, now);
    if (days !== null && days <= DUE_SOON_DAYS) return 'due_soon';
  }
  return TONES[state] ?? 'neutral';
}

function Icon({ tone }: { readonly tone: string }) {
  // Formas distintas por tono: el estado se distingue también sin color.
  const shape =
    tone === 'overdue' ? (
      <path d="M8 1.5 15 14.5H1Z M8 6v4 M8 11.5v1" />
    ) : tone === 'due_soon' ? (
      <>
        <circle cx="8" cy="8" r="6.5" />
        <path d="M8 4v4l3 2" />
      </>
    ) : tone === 'closed' ? (
      <>
        <circle cx="8" cy="8" r="6.5" />
        <path d="m5 8.5 2 2 4-4.5" />
      </>
    ) : tone === 'unclosable' ? (
      <path d="M8 1.5 14 4v4c0 3.5-2.6 5.8-6 6.5C4.6 13.8 2 11.5 2 8V4Z" />
    ) : tone === 'open' ? (
      <circle cx="8" cy="8" r="6.5" />
    ) : (
      <path d="M2.5 8h11" />
    );
  return (
    <svg
      className={styles.icon}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      data-icon={tone}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {shape}
    </svg>
  );
}

export interface StatusChipProps {
  /** `finding_lifecycle_state` que da el servidor. */
  readonly state: string;
  readonly overdueDays?: number;
  readonly dueAt?: string | null;
  /** Instante de referencia (`Clock`) para «vence dentro de»; sin él, el texto del estado. */
  readonly now?: number;
}

export function StatusChip({ state, overdueDays, dueAt, now }: StatusChipProps) {
  const tone = statusTone(state, dueAt, now);
  return (
    <span
      className={styles.chip}
      data-part="status_chip"
      data-state={known(state) ? state : 'unknown'}
      data-tone={tone}
    >
      <Icon tone={tone} />
      <span>
        {statusText(state, {
          ...(overdueDays === undefined ? {} : { overdueDays }),
          ...(dueAt === undefined ? {} : { dueAt }),
          ...(now === undefined ? {} : { now }),
        })}
      </span>
    </span>
  );
}

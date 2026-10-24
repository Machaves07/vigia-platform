/**
 * Iconos propios del semáforo: ojo abierto, entreabierto, cerrado y con interrogación. La forma
 * distingue el estado sin color (BR-APP-35); el icono es decorativo porque el texto lo nombra.
 */
import type { EyeIconKind } from './encoding';

const OUTLINE = 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z';

function Shape({ kind }: { kind: EyeIconKind }) {
  switch (kind) {
    case 'eye_open':
      return (
        <>
          <path d={OUTLINE} />
          <circle cx="12" cy="12" r="3" />
        </>
      );
    case 'eye_half':
      return (
        <>
          <path d="M2 12s3.5 7 10 7 10-7 10-7" />
          <path d="M2 12c2.5-2 6-3 10-3s7.5 1 10 3" />
          <path d="M9 12a3 3 0 0 0 6 0" />
        </>
      );
    case 'eye_closed':
      return (
        <>
          <path d="M2 10s3.5 5 10 5 10-5 10-5" />
          <path d="M5 13.5 3 16" />
          <path d="M12 15v3" />
          <path d="M19 13.5l2 2.5" />
        </>
      );
    case 'eye_unknown':
      return (
        <>
          <path d={OUTLINE} strokeDasharray="2 2" />
          <path d="M10 10a2 2 0 1 1 2.8 1.8c-.5.3-.8.7-.8 1.2" />
          <path d="M12 15.5v.01" />
        </>
      );
  }
}

export function EyeIcon({ kind, className }: { kind: EyeIconKind; className: string | undefined }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      data-icon={kind}
    >
      <Shape kind={kind} />
    </svg>
  );
}

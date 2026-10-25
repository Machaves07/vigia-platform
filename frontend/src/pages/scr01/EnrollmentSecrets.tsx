/**
 * Lo que la inscripción del segundo factor muestra una sola vez (IF-02, BR-APP-12): el código QR
 * que generó el servidor, la clave para escribirla a mano y los diez códigos de recuperación con
 * «lo guardé» obligatorio (UI-24).
 *
 * El QR llega como SVG de `qrcode` (un solo `<path>`); la política de contenido no admite
 * `innerHTML` ni imágenes `data:`, así que se toman solo `viewBox` y el trazo `d` con patrones
 * cerrados y se pintan con React. Si no tienen la forma esperada, solo queda la clave a mano.
 * Nada de esto pasa por la caché ni por el almacenamiento: vive en el estado de la página, que lo
 * suelta al confirmar o al salir.
 */
import type { ReactNode } from 'react';

import { CopyOnceSecret } from '@/components/CopyOnceSecret/CopyOnceSecret';
import type { EnrollmentView } from '@/ports/scr01Session';
import { text } from '@/texts';

import styles from './scr01.module.css';

const VIEW_BOX = /\bviewBox="(\d+(?:\.\d+)?(?: \d+(?:\.\d+)?){3})"/;
const PATH_DATA = /<path\b[^>]*?\sd="([MmLlHhVvZz0-9.,\s-]{1,200000})"/;

/** `viewBox` y trazo del QR del servidor, o `null` si el SVG no tiene la forma de `qrcode`. */
function qrShape(svg: string): { readonly viewBox: string; readonly d: string } | null {
  const viewBox = VIEW_BOX.exec(svg)?.[1];
  const d = PATH_DATA.exec(svg)?.[1];
  return viewBox === undefined || d === undefined ? null : { viewBox, d };
}

/** El secreto en base32 de la URI `otpauth://` para escribirlo a mano, o `null`. */
function manualKey(uri: string): string | null {
  if (!uri.startsWith('otpauth://')) return null;
  const query = uri.indexOf('?');
  if (query < 0) return null;
  const secret = new URLSearchParams(uri.slice(query + 1)).get('secret');
  return secret !== null && /^[A-Z2-7]+=*$/.test(secret) ? secret : null;
}

export function QrCode({ enrollment }: { readonly enrollment: EnrollmentView }): ReactNode {
  const shape = qrShape(enrollment.qr_svg);
  const key = manualKey(enrollment.provisioning_uri);
  return (
    <div className={styles.form}>
      {shape === null ? (
        <p className={styles.hint}>{text('scr01.enroll.qr_unavailable')}</p>
      ) : (
        <svg
          className={styles.qr}
          role="img"
          aria-label={text('scr01.enroll.qr')}
          viewBox={shape.viewBox}
        >
          <path d={shape.d} />
        </svg>
      )}
      {key === null ? null : (
        <p>
          {text('scr01.enroll.manual_key')}: <code className={styles.key}>{key}</code>
        </p>
      )}
    </div>
  );
}

/**
 * Códigos de recuperación hasta «lo guardé»; después, solo la constancia de que se guardaron.
 * La página suelta su copia en `onAcknowledged`.
 */
export function RecoveryCodes({
  codes,
  onAcknowledged,
}: {
  readonly codes: readonly string[] | null;
  readonly onAcknowledged: () => void;
}): ReactNode {
  if (codes === null) {
    return (
      <p role="status" data-text-key="scr01.enroll.codes_saved">
        {text('scr01.enroll.codes_saved')}
      </p>
    );
  }
  return <CopyOnceSecret kind="recovery_codes" value={codes} onAcknowledged={onAcknowledged} />;
}

/**
 * Lo que el servidor comprueba al cerrar el acta (IF-13 paso 6), con el sitio de la consola donde
 * se corrige. La pantalla no adelanta ninguna guarda (BR-APP-43): cuando el servidor rechaza el
 * cierre, su aviso queda **anclado** al requisito que falló (`false_negative_present`,
 * `steps_still_open`, `redundancy_not_verified`, `blur_not_verified`, `latency_not_measured`…),
 * con el enlace al grupo donde se resuelve.
 */
import { Link } from 'react-router';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { isPortError } from '@/ports/shared';
import { text } from '@/texts';

import styles from './close.module.css';

/** Requisitos del cierre: ancla del aviso, texto y grupo de la consola donde se corrige. */
const REQUIREMENTS: readonly {
  readonly anchor: string;
  readonly key: string;
  readonly link: { readonly group: string; readonly key: string } | null;
}[] = [
  {
    anchor: 'steps',
    key: 'scr06.close.requirement.steps',
    link: { group: '', key: 'scr06.close.to_session' },
  },
  {
    anchor: 'matrix',
    key: 'scr06.close.requirement.matrix',
    link: { group: '/matrix', key: 'scr06.session.to_matrix' },
  },
  {
    anchor: 'occlusion',
    key: 'scr06.close.requirement.occlusion',
    link: { group: '/occlusion', key: 'scr06.close.to_occlusion' },
  },
  { anchor: 'blur', key: 'scr06.close.requirement.blur', link: null },
  { anchor: 'latency', key: 'scr06.close.requirement.latency', link: null },
];

/** Campo de la aceptación de falsas alarmas (ruta del cuerpo de `CloseBody`). */
export const FALSE_ALARM_REASON = 'false_alarm_acceptance.reason_es';
/** Campo de las firmas. */
export const SIGNATURES = 'signatures';

/** `detail_code` de un rechazo del cierre → dónde se ancla su aviso. */
const DETAIL_ANCHORS: ReadonlyMap<string, string> = new Map([
  ['catalog_steps_still_open', 'steps'],
  ['catalog_walk_test_incomplete', 'steps'],
  ['catalog_matrix_incomplete', 'matrix'],
  ['catalog_false_negative_present', 'matrix'],
  ['catalog_passes_below_minimum', 'matrix'],
  ['catalog_redundancy_not_verified', 'occlusion'],
  ['catalog_blur_not_verified', 'blur'],
  ['catalog_latency_not_measured', 'latency'],
  ['catalog_false_alarm_rate_above_threshold', FALSE_ALARM_REASON],
  ['catalog_free_text_rejected', FALSE_ALARM_REASON],
]);

/** El ancla del aviso de un rechazo del cierre, o `null` si va a la pila global. */
export function closeAnchor(error: unknown): string | null {
  if (!isPortError(error) || error.failure.kind !== 'http') return null;
  const detail = error.failure.error.detail_code;
  // `Map` y no un objeto: un `detail_code` como `toString` no debe dar con el prototipo.
  return typeof detail === 'string' ? (DETAIL_ANCHORS.get(detail) ?? null) : null;
}

export function CloseRequirements({
  zoneId,
  noticeFor,
}: {
  readonly zoneId: string;
  readonly noticeFor: (field: string) => Notice | undefined;
}) {
  const base = `/zones/${encodeURIComponent(zoneId)}/commissioning`;
  return (
    <section className={styles.panel} aria-label={text('scr06.close.requirements.title')}>
      <h2 className={styles.subtitle}>{text('scr06.close.requirements.title')}</h2>
      <p className={styles.hint}>{text('scr06.close.requirements.hint')}</p>
      <ul className={styles.requirements}>
        {REQUIREMENTS.map((requirement) => {
          const notice = noticeFor(requirement.anchor);
          return (
            <li key={requirement.anchor} data-requirement={requirement.anchor}>
              <span>{text(requirement.key)}</span>
              {requirement.link === null ? null : (
                <>
                  {' '}
                  <Link to={`${base}${requirement.link.group}`}>{text(requirement.link.key)}</Link>
                </>
              )}
              {notice === undefined ? null : (
                <FieldNotice id={`close-${requirement.anchor}-${notice.id}`} notice={notice} />
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * O-18 (recorrido de cierre M10 de U-06): con el lazo cerrado, descartado o declarado incerrable,
 * la página de evidencia y cierre ya no ofrece los formularios. Arriba dice cómo terminó, cuándo y
 * quién firmó, y enlaza lo que sigue. Todo sale de lo que la página ya leyó (el detalle y el
 * expediente, o la respuesta del cierre recién firmado): ninguna lectura nueva.
 */
import { useEffect, useRef } from 'react';
import { Link } from 'react-router';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { recordContent, type Detail, type LedgerRecord } from '@/ports/scr09Closure';
import { text } from '@/texts';
import { closureSignature, loopAccess } from '../loopAccess';
import styles from './Closure.module.css';

export type EndedState = 'closed' | 'discarded' | 'unclosable';

export interface LoopEnding {
  readonly state: EndedState;
  /** Cuándo terminó (firma, clasificación o declaración); `null` si la página no lo tiene. */
  readonly at: string | null;
  /** Nombre de la firma o de quien clasificó; `null` si la página no lo tiene. */
  readonly by: string | null;
  /** El cierre se firmó en esta página: el aviso lo confirma y recibe el foco. */
  readonly justClosed: boolean;
}

/** El cierre que esta página acaba de firmar (la respuesta del servidor y la persona en sesión). */
export interface ClosedNow {
  readonly signedAt: string;
  readonly signer: string | null;
}

function signerName(content: Record<string, unknown>): string | null {
  const signer = content.signer;
  if (signer === null || typeof signer !== 'object' || Array.isArray(signer)) return null;
  const name = (signer as Record<string, unknown>).display_name;
  return typeof name === 'string' ? name : null;
}

/** La última declaración de incerrable del expediente, en el orden de la cadena. */
function lastDeclaration(records: readonly LedgerRecord[]) {
  const declarations = records
    .filter((record) => record.record_type === 'unclosable_state_declared')
    .sort((a, b) => a.chain_sequence - b.chain_sequence);
  const last = declarations.at(-1);
  if (last === undefined) return { at: null, by: null };
  const content = recordContent(last);
  return {
    at: typeof content.declared_at === 'string' ? content.declared_at : null,
    by: signerName(content),
  };
}

/** Cómo terminó el lazo, o `null` si sigue abierto y la página ofrece sus formularios. */
export function loopEnding(
  detail: Detail,
  records: readonly LedgerRecord[],
  closedNow: ClosedNow | null,
): LoopEnding | null {
  if (closedNow !== null) {
    return { state: 'closed', at: closedNow.signedAt, by: closedNow.signer, justClosed: true };
  }
  switch (detail.lifecycle_state) {
    case 'closed': {
      const signature = closureSignature(records);
      return {
        state: 'closed',
        at: signature?.signedAt ?? null,
        by: signature?.signer ?? null,
        justClosed: false,
      };
    }
    case 'discarded': {
      const last = [...detail.classifications]
        .sort((a, b) => a.sequence_in_subject - b.sequence_in_subject)
        .at(-1);
      return {
        state: 'discarded',
        at: last?.classified_at ?? null,
        by: last?.classified_by.display_name ?? null,
        justClosed: false,
      };
    }
    case 'unclosable':
      return { state: 'unclosable', ...lastDeclaration(records), justClosed: false };
    default:
      return null;
  }
}

export function LoopEnded({
  ending,
  findingId,
  permissions,
  timeZone,
}: {
  readonly ending: LoopEnding;
  readonly findingId: string;
  readonly permissions: readonly string[];
  readonly timeZone: string;
}) {
  const summary = useRef<HTMLParagraphElement>(null);
  // Recién firmado, el aviso recibe el foco: se ve y se anuncia sin buscarlo.
  useEffect(() => {
    if (ending.justClosed) summary.current?.focus();
  }, [ending.justClosed]);
  const canReopen =
    loopAccess(ending.state).includes('reopening') && permissions.includes('actions.manage');
  return (
    <section
      aria-label={text('scr09.closure.ended.label')}
      className={styles.notice}
      data-part="loop_ended"
      data-state={ending.state}
    >
      <p ref={summary} tabIndex={-1} role="status" data-part="ended_summary">
        {ending.justClosed && (
          <>
            <strong data-part="just_closed">{text('scr09.closure.ended.just_closed')}</strong>{' '}
          </>
        )}
        {text(`scr09.closure.ended.${ending.state}`)}
        {ending.at !== null && (
          <>
            {' '}
            {text('scr09.closure.ended.on')}{' '}
            <TimestampWithOffset instant={ending.at} timeZone={timeZone} />
          </>
        )}
        {ending.by !== null && (
          <>
            {' '}
            {text('scr09.closure.ended.by')} <span data-part="ended_by">{ending.by}</span>
          </>
        )}
      </p>
      <ul>
        {permissions.includes('root_cause.analyze') && (
          <li>
            <Link to={`/findings/${findingId}/root-cause`}>
              {text('scr09.closure.ended.root_cause')}
            </Link>
          </li>
        )}
        {permissions.includes('findings.read') && (
          <li>
            <Link to={`/findings/${findingId}/timeline`}>
              {text('scr09.closure.ended.timeline')}
            </Link>
          </li>
        )}
        {canReopen && (
          <li>
            <Link to={`/findings/${findingId}/loop/reopening`}>
              {text('scr09.closure.ended.reopen')}
            </Link>
          </li>
        )}
      </ul>
    </section>
  );
}

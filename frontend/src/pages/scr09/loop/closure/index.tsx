/**
 * SCR-09: evidencia, cierre firmado y vencidos siempre visibles, sin filtros. Con el lazo cerrado,
 * descartado o incerrable no se ofrecen los formularios: arriba, cómo terminó y lo que sigue (O-18).
 */
import { useRef, useState } from 'react';
import { useParams } from 'react-router';
import { text } from '@/texts';
import { ClosureScreen } from './common';
import { Evidence } from './Evidence';
import { Close } from './Close';
import { LoopEnded, loopEnding, type ClosedNow } from './Ended';
import { Overdue } from './Overdue';

export default function Page() {
  // El cierre que esta página acaba de firmar: el aviso no espera a la relectura del detalle.
  // Es del hallazgo en que se firmó: otro hallazgo en la misma página no lo hereda.
  const { finding_state_id: findingId = '' } = useParams();
  const [signed, setSigned] = useState<{
    readonly findingId: string;
    readonly closure: ClosedNow;
  } | null>(null);
  const closedNow = signed !== null && signed.findingId === findingId ? signed.closure : null;
  const signHeading = useRef<HTMLHeadingElement>(null);
  return (
    <ClosureScreen
      title={text('scr09.closure.title')}
      header={({ context, detail, records, timeZone }) => {
        const ending = loopEnding(detail, records, closedNow);
        return ending === null ? null : (
          <LoopEnded
            ending={ending}
            findingId={context.findingId}
            permissions={context.permissions}
            timeZone={timeZone}
          />
        );
      }}
    >
      {({ context, detail, timeZone, onWritten, records }) => {
        const ended = loopEnding(detail, records, closedNow) !== null;
        const canClose = context.permissions.includes('findings.close_and_sign');
        return (
          <>
            <Overdue context={context} detail={detail} timeZone={timeZone} records={records} />
            {!ended && context.permissions.includes('actions.manage') && (
              <Evidence
                detail={detail}
                timeZone={timeZone}
                records={records}
                onWritten={onWritten}
                {...(canClose
                  ? {
                      onNextStep: () => {
                        signHeading.current?.focus();
                      },
                    }
                  : {})}
              />
            )}
            {!ended && canClose && (
              <Close
                context={context}
                onWritten={onWritten}
                onClosed={(closure) => {
                  setSigned({
                    findingId: context.findingId,
                    closure: {
                      signedAt: closure.signed_at,
                      signer: context.principal?.display_name ?? null,
                    },
                  });
                }}
                headingRef={signHeading}
              />
            )}
          </>
        );
      }}
    </ClosureScreen>
  );
}

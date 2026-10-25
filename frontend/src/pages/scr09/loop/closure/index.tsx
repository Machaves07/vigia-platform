/** SCR-09: evidencia, cierre firmado y vencidos siempre visibles, sin filtros. */
import { text } from '@/texts';
import { ClosureScreen } from './common';
import { Evidence } from './Evidence';
import { Close } from './Close';
import { Overdue } from './Overdue';

export default function Page() {
  return (
    <ClosureScreen title={text('scr09.closure.title')}>
      {({ context, detail, timeZone, onWritten, records }) => (
        <>
          <Overdue context={context} detail={detail} timeZone={timeZone} records={records} />
          {context.permissions.includes('actions.manage') && (
            <Evidence detail={detail} timeZone={timeZone} onWritten={onWritten} />
          )}
          {context.permissions.includes('findings.close_and_sign') && (
            <Close context={context} onWritten={onWritten} />
          )}
        </>
      )}
    </ClosureScreen>
  );
}

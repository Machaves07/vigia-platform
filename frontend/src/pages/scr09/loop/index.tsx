/**
 * SCR-09, grupo `loop` (IF-09; SUP-U05-02 y 07): estado del lazo, sus registros en orden y la
 * acción sobre la zona. Los enlaces a evidencia y cierre, incerrable y reapertura (VIG-328)
 * aparecen según las claves que el servidor concede (`effective_permissions`), nunca por el
 * estado del hallazgo (BR-APP-43): si la transición no existe, el servidor lo dice al registrar.
 */
import { Link } from 'react-router';
import { canEnterGroup } from '@/app/navigation';
import { classifyPort } from '@/ports/scr09Classify';
import { text } from '@/texts';
import {
  LoopNav,
  plantTimeZone,
  Refresh,
  Result,
  useAuditedRead,
  useLoopContext,
  zoneName,
} from '../classify/common';
import { descriptor } from '../descriptor';
import { ActionForm } from './ActionForm';
import { LoopView } from './LoopView';

const NEXT = [
  { group: 'classify', path: 'classify', key: 'scr09.loop.classify' },
  { group: 'loop/closure', path: 'loop/closure', key: 'scr09.loop.closure' },
  { group: 'loop/unclosable', path: 'loop/unclosable', key: 'scr09.loop.unclosable' },
  { group: 'loop/reopening', path: 'loop/reopening', key: 'scr09.loop.reopening' },
] as const;

export default function Page() {
  const context = useLoopContext();
  const operation = 'GET /loop/findings/{finding_state_id}';
  const detail = useAuditedRead(context, operation, (signal) =>
    classifyPort.detail.read({ path: { finding_state_id: context.findingId } }, signal),
  );
  const granted = new Set(context.permissions);
  const links = NEXT.filter(({ group }) => {
    const route = descriptor.route_groups.find((candidate) => candidate.id === group);
    return route !== undefined && canEnterGroup(route, granted);
  });
  const canAssign =
    descriptor.sections
      .find((section) => section.id === 'action')
      ?.required_permissions.some((key) => granted.has(key)) ?? false;
  const data = detail.data;
  const timeZone = plantTimeZone(context, data?.plant_id);
  return (
    <main>
      <LoopNav findingId={context.findingId} />
      <h1>{text('scr09.loop.title')}</h1>
      <p>{text('scr09.classify.audited')}</p>
      <button type="button" disabled={detail.isFetching} onClick={detail.request}>
        {text('scr09.loop.load')}
      </button>
      {detail.requested && (
        <>
          <Refresh query={detail} operation={operation} />
          <Result query={detail}>
            {data !== undefined && (
              <>
                <LoopView detail={data} zone={zoneName(context, data.zone_id)} timeZone={timeZone}>
                  {links.length > 0 && (
                    <nav aria-label={text('scr09.loop.next')}>
                      <ul>
                        {links.map((link) => (
                          <li key={link.group}>
                            <Link to={`/findings/${context.findingId}/${link.path}`}>
                              {text(link.key)}
                            </Link>
                          </li>
                        ))}
                      </ul>
                    </nav>
                  )}
                </LoopView>
                {canAssign && (
                  <ActionForm
                    context={context}
                    plantId={data.plant_id}
                    timeZone={timeZone}
                    onWritten={detail.request}
                  />
                )}
              </>
            )}
          </Result>
        </>
      )}
    </main>
  );
}

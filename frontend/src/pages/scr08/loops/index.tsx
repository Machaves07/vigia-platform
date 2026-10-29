/**
 * SCR-08, grupo `loops`: «Lazos de cierre» del menú del coordinador (sistema visual v1, «Mi
 * trabajo»; VIG-365, S3-05). La bandeja con los estados de lazo vivo (`LOOP_STATES`), cada fila con
 * su siguiente paso; se abre con `findings.read`, como la bandeja.
 */
import { InboxScreen, LOOP_STATES } from '../inbox';

export default function Page() {
  return <InboxScreen title="scr08.loops.title" intro="scr08.loops.intro" states={LOOP_STATES} />;
}

/** RV-06 reutiliza el compositor compartido, sin proyectar identificadores ni sujetos al DOM. */
import { CoverageTimeline } from '@/components/CoverageTimeline/CoverageTimeline';
import { ScreenState } from '@/pages/shared/ScreenState';
import { belongsToOrganization } from '@/ports/scr03Integrity';
import type { Clock } from '@/state/clock';
import type { CoverageComposite, CoveragePeriod } from '@/state/coverage/compose';

export function CoverageView({
  data,
  organizationId,
  concessionOrganizationId = null,
  reportContractError,
  clock,
  timeZone = 'UTC',
  onPeriodChange = () => undefined,
}: {
  readonly data: { readonly organization_id: string; readonly composite: CoverageComposite };
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { code: string }) => void;
  readonly clock: Clock;
  readonly timeZone?: string;
  readonly onPeriodChange?: (period: CoveragePeriod) => void;
}) {
  if (
    !belongsToOrganization(data.organization_id, {
      organizationId,
      concessionOrganizationId,
      ...(reportContractError === undefined ? {} : { reportContractError }),
    })
  )
    return <ScreenState state="error" />;
  return (
    <CoverageTimeline
      composite={data.composite}
      timeZone={timeZone}
      clock={clock}
      onPeriodChange={onPeriodChange}
    />
  );
}

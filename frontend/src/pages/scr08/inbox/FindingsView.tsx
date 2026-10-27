/** RV-11: selección explícita de campos; nunca serializa el objeto recibido. */
import { Link } from 'react-router';
import { ObservabilityBadge } from '@/components/ObservabilityBadge/ObservabilityBadge';
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import { FindingActions } from '@/pages/shared/FindingActions';
import {
  gateDisplay,
  type Finding,
  type InboxPage,
  type Plant,
  type ZoneGate,
} from '@/ports/scr08Findings';
import { text } from '@/texts';
import { FindingTime } from '../shared';
import styles from './inbox.module.css';

export const FAMILIES = ['coexistence', 'guard_bypass', 'dwell', 'startup_transition'] as const;
export const TIERS = ['tier_1', 'tier_2'] as const;
export const STATES = [
  'received',
  'in_review',
  'discarded',
  'open',
  'action_assigned',
  'overdue',
  'closed',
  'unclosable',
  'suspended_by_reclassification',
] as const;
export function findingLabel(kind: 'family' | 'tier' | 'state', value: string) {
  const allowed: readonly string[] =
    kind === 'family' ? FAMILIES : kind === 'tier' ? TIERS : STATES;
  return text(allowed.includes(value) ? `scr08.inbox.${kind}.${value}` : 'scr08.inbox.unknown');
}

const ZONE_NOTICES = ['coverage_unavailable', 'not_observed', 'no_node_assigned'] as const;
const PARTITION_NOTICES = ['projection_rebuilding', 'projection_diverged'] as const;
const FINDING_NOTICES = [
  'agreement_revoked',
  'walk_test_regression_pending',
  'evidence_flagged',
  'missing_segments_declared',
] as const;

function Notices({ finding, timeZone }: { readonly finding: Finding; readonly timeZone: string }) {
  return (
    <>
      {finding.notices.map((notice, index) =>
        notice.code === 'agreement_revoked' ? (
          <GateStateChip
            key={index}
            gate={{ state: 'agreement_revoked', agreement_revoked_at: notice.since }}
            timeZone={timeZone}
          />
        ) : (
          <p key={index}>
            {text(
              (FINDING_NOTICES as readonly string[]).includes(notice.code)
                ? `scr08.inbox.notice.${notice.code}`
                : 'scr08.inbox.unknown',
            )}
            {notice.since !== null && (
              <>
                {' '}
                <FindingTime instant={notice.since} timeZone={timeZone} />
              </>
            )}
          </p>
        ),
      )}
    </>
  );
}

const NO_PERMISSIONS: ReadonlySet<string> = new Set();

export function FindingsView({
  page,
  plants,
  gates,
  permissions = NO_PERMISSIONS,
}: {
  readonly page: InboxPage;
  readonly plants: readonly Plant[];
  readonly gates: readonly ZoneGate[];
  /** Claves de la sesión: cada fila ofrece el acceso de su estado que la sesión abre (VIG-359). */
  readonly permissions?: ReadonlySet<string>;
}) {
  const zoneName = (id: string) =>
    plants.flatMap((p) => p.zones).find((z) => z.zone_id === id)?.name ?? id;
  return (
    <>
      {page.partitions.map((partition) => (
        <p role="status" key={partition.plant_id}>
          {plants.find((p) => p.plant_id === partition.plant_id)?.name ?? partition.plant_id}
          {': '}
          {text(
            (PARTITION_NOTICES as readonly string[]).includes(partition.notice)
              ? `scr08.inbox.notice.${partition.notice}`
              : 'scr08.inbox.unknown',
          )}
        </p>
      ))}
      {page.zones.map((zone) => (
        <p role="status" key={zone.zone_id}>
          {zoneName(zone.zone_id)}
          {': '}
          {text(
            (ZONE_NOTICES as readonly string[]).includes(zone.notice)
              ? `scr08.inbox.notice.${zone.notice}`
              : 'scr08.inbox.unknown',
          )}
        </p>
      ))}
      {page.items.length > 0 && (
        <div className={styles.scroll}>
          <table>
            <caption>{text('scr08.inbox.episodes')}</caption>
            <thead>
              <tr>
                {[
                  'zone',
                  'family',
                  'tier',
                  'state',
                  'age',
                  'observability',
                  'standard',
                  'duration',
                  'clips',
                  'occurred',
                  'due',
                ].map((key) => (
                  <th scope="col" key={key}>
                    {text(`scr08.inbox.${key}`)}
                  </th>
                ))}
                <th scope="col">{text('finding_journey.navigation')}</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((finding) => {
                const plant = plants.find((p) => p.plant_id === finding.plant_id);
                const timeZone = plant?.timezone ?? '';
                const status = gates.find((g) => g.zone_id === finding.zone_id);
                const display =
                  status === undefined
                    ? { state: 'unknown' }
                    : gateDisplay(status.gate, status.regression);
                const revoked = finding.notices.some(
                  (notice) => notice.code === 'agreement_revoked',
                );
                if (display.state !== 'productive' && !revoked)
                  return (
                    <tr key={finding.finding_state_id}>
                      <td colSpan={12}>
                        {zoneName(finding.zone_id)}{' '}
                        <GateStateChip gate={display} timeZone={timeZone} />
                      </td>
                    </tr>
                  );
                return (
                  <tr key={finding.finding_state_id}>
                    <td>
                      <Link to={`/findings/${finding.finding_state_id}`}>
                        {zoneName(finding.zone_id)}
                      </Link>
                      <Notices finding={finding} timeZone={timeZone} />
                    </td>
                    <td>{findingLabel('family', finding.family)}</td>
                    <td>{findingLabel('tier', finding.tier)}</td>
                    <td>{findingLabel('state', finding.lifecycle_state)}</td>
                    <td>
                      {finding.age_days} {text('scr08.inbox.days')}
                    </td>
                    <td>
                      <ObservabilityBadge state={finding.observability_state_at_event} />
                    </td>
                    <td>
                      {text('scr08.inbox.version')} {finding.standard.version}
                    </td>
                    <td>{text('scr08.inbox.data_unavailable')}</td>
                    <td>{text('scr08.inbox.data_unavailable')}</td>
                    <td>
                      <FindingTime instant={finding.occurred_at} timeZone={timeZone} />
                      {finding.time_aggregated && <p>{text('scr08.inbox.aggregated')}</p>}
                    </td>
                    <td>
                      {finding.due_at === null ? (
                        text('scr08.inbox.no_due')
                      ) : (
                        <FindingTime instant={finding.due_at} timeZone={timeZone} />
                      )}
                    </td>
                    <td>
                      <FindingActions
                        findingId={finding.finding_state_id}
                        state={finding.lifecycle_state}
                        permissions={permissions}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

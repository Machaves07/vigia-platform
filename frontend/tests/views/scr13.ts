/** RV-17 registrada: métricas del programa, nunca del usuario, del equipo o del turno. */
import { createElement } from 'react';
import { MetricView, AdoptionView, OverdueView, ExposureView } from '@/pages/scr13/Views';
import type { MetricSnapshot, Adoption, Overdue, Exposure } from '@/ports/scr13Metrics';
import { composeCoverage } from '@/state/coverage/compose';
import { FakeClock } from '../support/fakeClock';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  zoneExposure: {
    screen: 'SCR-13',
    view: 'RV-17',
    data: readView('ExposureOut').map((data) => ({
      ...data,
      period_from: '2026-01-01T00:00:00.000Z',
      period_to: '2026-02-01T00:00:00.000Z',
      coverage_timeline: {
        ...(data.coverage_timeline as Record<string, unknown>),
        period: { from: '2026-01-01T00:00:00.000Z', to: '2026-02-01T00:00:00.000Z' },
      },
    })),
    render: (data) => {
      const exposure = data as unknown as Exposure;
      const period = {
        fromMs: Date.parse(exposure.period_from),
        toMs: Date.parse(exposure.period_to),
      };
      const span = period.toMs - period.fromMs;
      const composite = composeCoverage(period, [
        {
          period,
          response: {
            period: { from: exposure.period_from, to: exposure.period_to },
            intervals: [
              {
                starts_at: exposure.period_from,
                ends_at: exposure.period_to,
                duration_ms: span,
                layer: 'node_report',
                state: 'observable',
                causes: [],
                clock_basis: 'node',
              },
            ],
            summary: {
              observable_ms: span,
              degraded_ms: 0,
              not_observable_ms: 0,
              no_communication_ms: 0,
              never_reported_ms: 0,
              zone_not_active_ms: 0,
            },
          },
        },
      ]);
      return createElement(ExposureView, {
        data: exposure,
        composite,
        clock: new FakeClock(),
        timeZone: 'UTC',
        onPeriodChange: () => undefined,
      });
    },
    allowedFields: [
      'period',
      'period_from',
      'period_to',
      'confirmed_minutes',
      'pending_classification_minutes',
      'observable_coverage_percent',
      'hourly_distribution',
      'start_minute_of_day',
      'width_minutes',
      'coverage_timeline',
      'annotations',
      'kind',
      'from',
      'to',
    ],
  },
  programMetrics: {
    screen: 'SCR-13',
    view: 'RV-17',
    data: readView('ProgramMetricSnapshot').map((data) => ({
      ...data,
      period: '2026-01',
      values: [
        ...new Map(
          (data.values as MetricSnapshot['values']).map((metric) => [metric.key, metric]),
        ).values(),
      ],
    })),
    render: (data, context) =>
      createElement(MetricView, {
        data: data as unknown as MetricSnapshot,
        zoneName: 'Zona sintética',
        ...context,
      }),
    allowedFields: ['period', 'values', 'key', 'status', 'unit', 'value'],
    organizationOf: 'organization_id',
  },
  adoptionByZone: {
    screen: 'SCR-13',
    view: 'RV-17',
    data: readView('AdoptionReportOut'),
    render: (data) =>
      createElement(AdoptionView, {
        data: data as unknown as Adoption,
        zoneName: 'Zona sintética',
      }),
    allowedFields: ['zones', 'silent_days', 'days', 'day', 'activity_count'],
  },
  weeklyOverdue: {
    screen: 'SCR-13',
    view: 'RV-17',
    data: readView('OverduePageOut'),
    render: (data) =>
      createElement(OverdueView, { data: data as unknown as Overdue, zoneName: 'Zona sintética' }),
    allowedFields: ['zones', 'overdue_count', 'items', 'overdue_days'],
  },
} satisfies Record<string, ViewRegistration>;

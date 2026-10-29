/**
 * UI-10, UI-17, UI-20 y UI-25 (LC-APP-19): `axe` sin violaciones serias, el semáforo se entiende
 * sin color por su nombre accesible, los seis `gate_display_state`, la hora de la planta con la
 * variante del nodo y la planta de la fila, «actualizado hace X» y el aviso de carga prolongada
 * a 1 s exacto del `FakeClock`. Se comparan textos del catálogo, nunca subcadenas.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { FakeClock } from '../support/fakeClock';
import { GateStateChip } from '@/components/GateStateChip/GateStateChip';
import {
  ObservabilityBadge,
  ObservabilityLegend,
} from '@/components/ObservabilityBadge/ObservabilityBadge';
import { LoadingSkeleton } from '@/components/RefreshIndicator/LoadingSkeleton';
import { RefreshIndicator } from '@/components/RefreshIndicator/RefreshIndicator';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import catalog from '@/texts/components_status.json';

type CatalogKey = keyof typeof catalog;
const t = (key: CatalogKey): string => catalog[key];

const BOGOTA = 'America/Bogota';
/** 2026-10-23 14:05:09 en Bogotá (UTC−05:00). */
const INSTANT = '2026-10-23T19:05:09.250Z';

function part(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[data-part="${name}"]`);
  if (element === null) throw new Error(`Falta [data-part="${name}"]`);
  return element;
}

async function expectNoSeriousViolations(ui: ReactElement) {
  const { container } = render(<main>{ui}</main>);
  const results = await axe(container);
  const serious = results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
  expect(serious).toEqual([]);
  expect(results).toHaveNoViolations();
}

describe('axe en los cuatro componentes', () => {
  it.each([
    ['ObservabilityBadge', <ObservabilityBadge key="b" state="degraded" cause="obstruction" />],
    [
      'ObservabilityBadge con desde',
      <ObservabilityBadge
        key="s"
        state="observable"
        since={{ instant: INSTANT, timeZone: BOGOTA }}
      />,
    ],
    [
      'GateStateChip',
      <GateStateChip key="g" gate={{ state: 'commissioning' }} timeZone={BOGOTA} />,
    ],
    [
      'TimestampWithOffset',
      <TimestampWithOffset
        key="t"
        instant={INSTANT}
        timeZone={BOGOTA}
        nodeTime="2026-10-23T19:05:11.550Z"
        plantName="Planta Norte"
      />,
    ],
    [
      'RefreshIndicator',
      <RefreshIndicator
        key="r"
        fetchedAt={0}
        refreshPolicy="on_demand"
        onRefresh={() => undefined}
        clock={new FakeClock(0)}
      />,
    ],
    ['LoadingSkeleton', <LoadingSkeleton key="l" clock={new FakeClock(0)} />],
  ])('%s', async (_name, ui) => {
    await expectNoSeriousViolations(ui);
  });
});

describe('UI-10 ObservabilityBadge', () => {
  it.each([
    ['observable', 'components_status.observability.observable', 'eye_open'],
    ['degraded', 'components_status.observability.degraded', 'eye_half'],
    ['not_observable', 'components_status.observability.not_observable', 'eye_closed'],
    ['safe', 'components_status.observability.unknown', 'eye_unknown'],
  ] as const)('%s se entiende sin color: nombre accesible e icono propio', (state, key, icon) => {
    const { container } = render(<ObservabilityBadge state={state} />);
    const group = screen.getByRole('group', {
      name: `${t('components_status.observability.group_label')}: ${t(key)}`,
    });
    expect(group.getAttribute('data-state')).toBe(state === 'safe' ? 'unknown' : state);
    expect(container.querySelector('svg')?.getAttribute('data-icon')).toBe(icon);
    expect(container.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
    // VIG-365 (S3-04, S3-07): la leyenda va solo con «Observable», donde podría leerse «despejada».
    const legend = container.querySelector('[data-part="legend"]');
    expect(legend?.textContent ?? null).toBe(
      state === 'observable' ? t('components_status.observability.legend') : null,
    );
  });

  it('en una tabla, la celda no repite la leyenda: va una vez con ObservabilityLegend', () => {
    const { container } = render(
      <>
        <ObservabilityBadge state="observable" inTable />
        <ObservabilityBadge state="observable" inTable />
        <ObservabilityLegend />
      </>,
    );
    expect(
      [...container.querySelectorAll('[data-part="legend"]')].map((node) => node.textContent),
    ).toEqual([t('components_status.observability.legend')]);
  });

  it('cada estado tiene un icono distinto', () => {
    const icons = ['observable', 'degraded', 'not_observable', 'x'].map((state) => {
      const { container, unmount } = render(<ObservabilityBadge state={state} />);
      const shape = container.querySelector('svg')?.innerHTML;
      unmount();
      return shape;
    });
    expect(new Set(icons).size).toBe(4);
  });

  it('la causa usa la etiqueta neutral de su lista; una causa sin etiqueta no muestra el valor crudo', () => {
    const { container, rerender } = render(<ObservabilityBadge state="degraded" cause="focus" />);
    expect(part(container, 'cause').textContent).toBe(
      `${t('components_status.observability.cause')}: desenfoque`,
    );
    rerender(<ObservabilityBadge state="not_observable" cause="no_communication" />);
    expect(part(container, 'cause').textContent).toBe(
      `${t('components_status.observability.cause')}: sin comunicación con el nodo`,
    );
    rerender(<ObservabilityBadge state="degraded" cause="sabotage" />);
    expect(part(container, 'cause').textContent).toBe(
      `${t('components_status.observability.cause')}: ${t('components_status.observability.cause_unknown')}`,
    );
  });

  it('«desde» va en la zona de la planta', () => {
    const { container } = render(
      <ObservabilityBadge state="degraded" since={{ instant: INSTANT, timeZone: BOGOTA }} />,
    );
    const since = part(container, 'since');
    expect(since.querySelector('time')?.textContent).toBe('23/10/2026, 14:05:09');
    expect(since.querySelector('[data-part="zone"]')?.textContent).toBe(
      '(America/Bogota, UTC-05:00)',
    );
  });
});

describe('UI-17 GateStateChip', () => {
  it.each([
    ['no_scope_record', 'components_status.gate.no_scope_record'],
    ['commissioning', 'components_status.gate.commissioning'],
    ['no_use_agreement', 'components_status.gate.no_use_agreement'],
    ['regression_pending', 'components_status.gate.regression_pending'],
    ['productive', 'components_status.gate.productive'],
    ['zone_clear', 'components_status.gate.unknown'],
    ['constructor', 'components_status.gate.unknown'],
  ] as const)('%s muestra su texto del catálogo', (state, key) => {
    render(<GateStateChip gate={{ state }} timeZone={BOGOTA} />);
    const chip = screen.getByRole('group', { name: t('components_status.gate.group_label') });
    expect(chip.textContent).toBe(t(key));
    expect(chip.getAttribute('data-state')).toBe(key.endsWith('unknown') ? 'unknown' : state);
  });

  it('acuerdo revocado desde <fecha> en la zona de la planta', () => {
    render(
      <GateStateChip
        gate={{ state: 'agreement_revoked', agreement_revoked_at: INSTANT }}
        timeZone={BOGOTA}
      />,
    );
    const chip = screen.getByRole('group', { name: t('components_status.gate.group_label') });
    expect(chip.textContent).toBe(
      `${t('components_status.gate.agreement_revoked')} 23/10/2026, 14:05:09 (America/Bogota, UTC-05:00)`,
    );
    expect(chip.querySelector('time')?.getAttribute('dateTime')).toBe(INSTANT);
  });

  it('acuerdo revocado sin fecha no inventa una', () => {
    render(
      <GateStateChip
        gate={{ state: 'agreement_revoked', agreement_revoked_at: null }}
        timeZone={BOGOTA}
      />,
    );
    expect(
      screen.getByRole('group', { name: t('components_status.gate.group_label') }).textContent,
    ).toBe(t('components_status.gate.agreement_revoked_without_date'));
  });
});

describe('UI-25 TimestampWithOffset', () => {
  it('la hora del nodo va en la misma zona, con su desfase', () => {
    const { container } = render(
      <TimestampWithOffset
        instant={INSTANT}
        timeZone={BOGOTA}
        nodeTime="2026-10-23T19:05:11.550Z"
      />,
    );
    const node = part(container, 'node');
    expect(node.querySelector('time')?.textContent).toBe('23/10/2026, 14:05:11');
    expect(part(container, 'offset').textContent).toBe(
      ` · ${t('components_status.timestamp.node_offset')} +2,3 s`,
    );
  });

  it.each([
    ['2026-10-23T19:05:09.250Z', '0 s'],
    ['2026-10-23T19:04:59.250Z', '-10 s'],
  ])('desfase de %s', (nodeTime, expected) => {
    const { container } = render(
      <TimestampWithOffset instant={INSTANT} timeZone={BOGOTA} nodeTime={nodeTime} />,
    );
    expect(part(container, 'offset').textContent).toBe(
      ` · ${t('components_status.timestamp.node_offset')} ${expected}`,
    );
  });

  it('con «todas las plantas» la fila indica su planta', () => {
    const { container } = render(
      <TimestampWithOffset instant={INSTANT} timeZone="Europe/Madrid" plantName="Planta Sur" />,
    );
    expect(part(container, 'plant').textContent).toBe(
      `${t('components_status.timestamp.plant')}: Planta Sur`,
    );
    expect(part(container, 'zone').textContent).toBe('(Europe/Madrid, UTC+02:00)');
    expect(container.querySelector('time')?.textContent).toBe('23/10/2026, 21:05:09');
  });

  it('la hora repetida del cambio de horario se distingue por el desplazamiento', () => {
    const first = render(
      <TimestampWithOffset instant="2026-11-01T05:30:00.000Z" timeZone="America/New_York" />,
    );
    const second = render(
      <TimestampWithOffset instant="2026-11-01T06:30:00.000Z" timeZone="America/New_York" />,
    );
    expect(first.container.querySelector('time')?.textContent).toBe('01/11/2026, 01:30:00');
    expect(second.container.querySelector('time')?.textContent).toBe('01/11/2026, 01:30:00');
    expect(part(first.container, 'zone').textContent).toBe('(America/New_York, UTC-04:00)');
    expect(part(second.container, 'zone').textContent).toBe('(America/New_York, UTC-05:00)');
  });

  it('un desplazamiento con segundos se muestra completo', () => {
    const { container } = render(
      <TimestampWithOffset instant="1971-01-01T12:00:00.000Z" timeZone="Africa/Monrovia" />,
    );
    expect(container.querySelector('time')?.textContent).toBe('01/01/1971, 11:15:30');
    expect(part(container, 'zone').textContent).toBe('(Africa/Monrovia, UTC-00:44:30)');
  });

  it.each([
    ['instante no válido', 'ayer', BOGOTA],
    ['zona no válida', INSTANT, 'Mars/Olympus'],
    ['zona vacía', INSTANT, ''],
  ])('%s: «Fecha no disponible», nunca otra zona', (_name, instant, timeZone) => {
    const { container } = render(<TimestampWithOffset instant={instant} timeZone={timeZone} />);
    expect(part(container, 'platform').textContent).toBe(
      t('components_status.timestamp.unavailable'),
    );
    expect(container.querySelector('time')).toBeNull();
  });
});

describe('UI-20 RefreshIndicator', () => {
  const START = Date.UTC(2026, 9, 23, 19, 0, 0);

  it('«actualizado hace X» con el reloj inyectado, recalculado con sus temporizadores', () => {
    const clock = new FakeClock(START);
    const { container } = render(
      <RefreshIndicator
        fetchedAt={START}
        refreshPolicy="periodic_60s"
        onRefresh={() => undefined}
        clock={clock}
      />,
    );
    const updated = () => part(container, 'updated').textContent;
    expect(updated()).toBe(`${t('components_status.refresh.updated')} ahora`);
    act(() => {
      clock.advance(10_000);
    });
    expect(updated()).toBe(`${t('components_status.refresh.updated')} hace 10 segundos`);
    act(() => {
      clock.advance(170_000);
    });
    expect(updated()).toBe(`${t('components_status.refresh.updated')} hace 3 minutos`);
    act(() => {
      clock.advance(2 * 3_600_000);
    });
    expect(updated()).toBe(`${t('components_status.refresh.updated')} hace 2 horas`);
    expect(part(container, 'policy').textContent).toBe(t('components_status.refresh.periodic_60s'));
  });

  it('sin respuesta aún, a demanda; el botón pide la actualización y se deshabilita en curso', () => {
    const onRefresh = vi.fn();
    const clock = new FakeClock(START);
    const { container, rerender } = render(
      <RefreshIndicator
        fetchedAt={null}
        refreshPolicy="on_demand"
        onRefresh={onRefresh}
        clock={clock}
      />,
    );
    expect(part(container, 'updated').textContent).toBe(t('components_status.refresh.never'));
    expect(part(container, 'policy').textContent).toBe(t('components_status.refresh.on_demand'));
    const button = screen.getByRole('button', { name: t('components_status.refresh.button') });
    fireEvent.click(button);
    expect(onRefresh).toHaveBeenCalledTimes(1);
    rerender(
      <RefreshIndicator
        fetchedAt={null}
        refreshPolicy="on_demand"
        onRefresh={onRefresh}
        clock={clock}
        isFetching
      />,
    );
    expect(button).toHaveProperty('disabled', true);
  });

  it('una respuesta con hora futura no muestra un tiempo negativo', () => {
    render(
      <RefreshIndicator
        fetchedAt={START + 60_000}
        refreshPolicy="on_demand"
        onRefresh={() => undefined}
        clock={new FakeClock(START)}
      />,
    );
    expect(screen.getByText(`${t('components_status.refresh.updated')} ahora`)).toBeDefined();
  });

  it('desmontar cancela su temporizador', () => {
    const clock = new FakeClock(START);
    const { unmount } = render(
      <RefreshIndicator
        fetchedAt={START}
        refreshPolicy="on_demand"
        onRefresh={() => undefined}
        clock={clock}
      />,
    );
    expect(clock.pendingCount).toBe(1);
    unmount();
    expect(clock.pendingCount).toBe(0);
  });
});

describe('NFR-APP-53: esqueleto inmediato y carga prolongada a 1 s', () => {
  it('el aviso aparece a los 1000 ms del FakeClock y no a los 999', () => {
    const clock = new FakeClock(0);
    const { container } = render(<LoadingSkeleton clock={clock} />);
    const status = screen.getByRole('status');
    expect(status.getAttribute('aria-busy')).toBe('true');
    expect(status.querySelector('p')?.textContent).toBe(t('components_status.loading.label'));
    expect(container.querySelector('[data-part="long-load"]')).toBeNull();
    act(() => {
      clock.advance(999);
    });
    expect(container.querySelector('[data-part="long-load"]')).toBeNull();
    act(() => {
      clock.advance(1);
    });
    expect(part(container, 'long-load').textContent).toBe(t('components_status.loading.long'));
  });

  it('desmontar antes de 1 s cancela el aviso', () => {
    const clock = new FakeClock(0);
    const { unmount } = render(<LoadingSkeleton clock={clock} />);
    expect(clock.pendingCount).toBe(1);
    unmount();
    expect(clock.pendingCount).toBe(0);
  });
});

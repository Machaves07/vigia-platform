/**
 * PR-APP-08 (BR-APP-23, 35): para todo `observability_state` y toda causa, el semáforo muestra
 * texto no vacío del catálogo, un icono y un color del conjunto {azul, ámbar, gris} leído de las
 * variables CSS, nunca verde ni rojo, y siempre la leyenda. Los valores fuera de lista también.
 */
import { render } from '@testing-library/react';
import fc from 'fast-check';
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';

import platform from '../../../backend/resources/labels.platform.es.json';
import { allBadgeColors, colorFamily, toneBackground } from '../support/semaphoreColors';
import { ObservabilityBadge } from '@/components/ObservabilityBadge/ObservabilityBadge';
import { labels } from '@/contracts';
import catalog from '@/texts/components_status.json';

const STATES = ['observable', 'degraded', 'not_observable'] as const;
const EXPECTED: Readonly<Record<string, { family: string; icon: string; text: string }>> = {
  observable: {
    family: 'blue',
    icon: 'eye_open',
    text: catalog['components_status.observability.observable'],
  },
  degraded: {
    family: 'amber',
    icon: 'eye_half',
    text: catalog['components_status.observability.degraded'],
  },
  not_observable: {
    family: 'grey',
    icon: 'eye_closed',
    text: catalog['components_status.observability.not_observable'],
  },
};
const CAUSES = [...Object.keys(labels.degradation_cause), ...Object.keys(platform.platform_cause)];
const CAUSE_LABELS: Readonly<Record<string, string>> = {
  ...labels.degradation_cause,
  ...platform.platform_cause,
};
const catalogTexts = new Set<string>(Object.values(catalog));

const state = fc.oneof(
  { weight: 3, arbitrary: fc.constantFrom(...STATES) },
  { weight: 1, arbitrary: fc.string() },
);
const cause = fc.option(fc.oneof(fc.constantFrom(...CAUSES), fc.string()), { nil: undefined });

function part(container: HTMLElement, name: string): HTMLElement | null {
  return container.querySelector<HTMLElement>(`[data-part="${name}"]`);
}

describe('PR-APP-08: semáforo sin verde ni rojo, con texto, icono y leyenda', () => {
  it('cada tono del módulo CSS es azul, ámbar o gris, y ningún color del módulo es verde ni rojo', () => {
    expect(colorFamily(toneBackground('blue'))).toBe('blue');
    expect(colorFamily(toneBackground('amber'))).toBe('amber');
    expect(colorFamily(toneBackground('grey'))).toBe('grey');
    expect(colorFamily(toneBackground('neutral'))).toBe('grey');
    for (const color of allBadgeColors()) {
      expect(['green', 'red']).not.toContain(colorFamily(color));
    }
  });

  it('para todo estado y causa: texto del catálogo, icono, color permitido y leyenda', () => {
    fc.assert(
      fc.property(state, cause, (value, causeValue) => {
        const props =
          causeValue === undefined ? { state: value } : { state: value, cause: causeValue };
        const { container, unmount } = render(createElement(ObservabilityBadge, props));
        try {
          const expected = (Object.hasOwn(EXPECTED, value) ? EXPECTED[value] : undefined) ?? {
            family: 'grey',
            icon: 'eye_unknown',
            text: catalog['components_status.observability.unknown'],
          };
          const stateText = part(container, 'state')?.textContent ?? '';
          expect(stateText).toBe(expected.text);
          expect(stateText.trim()).not.toBe('');
          expect(catalogTexts.has(stateText)).toBe(true);

          const icon = container.querySelector('svg[data-icon]');
          expect(icon?.getAttribute('data-icon')).toBe(expected.icon);
          expect(icon?.querySelector('path, circle')).not.toBeNull();

          const tone = container.querySelector('[data-tone]')?.getAttribute('data-tone') ?? '';
          const family = colorFamily(toneBackground(tone));
          expect(family).toBe(expected.family);
          expect(['blue', 'amber', 'grey']).toContain(family);

          // La leyenda va junto a «Observable», el estado que podría leerse «despejada», y no con
          // los demás, donde contradecía lo que se ve (decisión del dueño en VIG-365, S3-07).
          expect(part(container, 'legend')?.textContent ?? null).toBe(
            value === 'observable' ? catalog['components_status.observability.legend'] : null,
          );

          const causeNode = part(container, 'cause');
          if (causeValue === undefined) {
            expect(causeNode).toBeNull();
          } else {
            const label = Object.hasOwn(CAUSE_LABELS, causeValue)
              ? CAUSE_LABELS[causeValue]
              : catalog['components_status.observability.cause_unknown'];
            expect(causeNode?.textContent).toBe(
              `${catalog['components_status.observability.cause']}: ${String(label)}`,
            );
          }
        } finally {
          unmount();
        }
      }),
    );
  });
});

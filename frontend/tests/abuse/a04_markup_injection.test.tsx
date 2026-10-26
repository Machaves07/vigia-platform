import { screen } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { expect, it } from 'vitest';
import { text } from '@/texts';
import { classification, detail, fixture, history, WAIT } from '../components/scr08/detail_support';

it.each([
  '<img src=x onerror="globalThis.abuseExecuted=true">',
  '&lt;script&gt;alert(1)&lt;/script&gt;',
  '<svg onload="alert(1)"></svg>',
])(
  'A-4 · BR-APP-42, 70: muestra como texto el marcado hostil %s',
  async (payload) => {
    const env = await fixture();
    const value = detail();
    value.standard.declared_text = payload;
    env.responses.detail = () => HttpResponse.json(value);
    env.responses.history = () =>
      HttpResponse.json(history([{ ...classification(1), reason_text: payload }]));
    const view = env.view();
    await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
    await screen.findByText(payload, { selector: '[data-part="reason_text"]' }, WAIT);
    expect(view.container.querySelector('[data-part="declared_text"]')?.textContent).toBe(payload);
    expect(view.container.querySelector('script, img, svg[onload], [onerror]')).toBeNull();
    expect(Object.hasOwn(globalThis, 'abuseExecuted')).toBe(false);
  },
  30_000,
);

/**
 * UI-18 `LiveViewLauncher` (BR-APP-65 y 66, PAT-APP-SEG-03, LC-APP-18, PR-APP-20 y 29).
 *
 * Pantalla previa por **claves** del catálogo (nunca por subcadenas de texto renderizado), enlace
 * al nodo con el token solo en el fragmento, cuenta regresiva de 600 s con `FakeClock`, eliminación
 * del enlace al pulsarlo o al vencer, `rate_limited` que deshabilita «abrir de nuevo» exactamente
 * `retry_after_seconds`, y `window.open` que nunca se llama. La red es MSW y el transporte, el real
 * de `ports.base`.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import {
  LiveViewLauncher,
  type LiveViewOpened,
} from '@/components/LiveViewLauncher/LiveViewLauncher';
import { installTransport } from '@/ports/base';
import { LIVE_VIEW_TOKEN_SECONDS } from '@/state/reducers/countdown';
import { text } from '@/texts';
import catalog from '@/texts/live_view.json';

import { server } from '../setup/msw';
import {
  auxClick,
  EXPIRES_AT,
  handoff,
  liveViewHarness,
  LOCAL_URL,
  TOKEN,
  tokenFoundIn,
  ZONE_ID,
  type LiveViewHarness,
} from '../support/liveView';

/** Las cuatro causas de rechazo del nodo (nota U05-H-07 de BR-APP-66). */
const NODE_REJECTION_KEYS = [
  'live_view.rejection.limit',
  'live_view.rejection.mode',
  'live_view.rejection.reused',
  'live_view.rejection.expired',
] as const;
/** El aviso del certificado autofirmado de transición y su instrucción. */
const CERTIFICATE_KEYS = [
  'live_view.certificate_notice',
  'live_view.certificate_instruction',
] as const;
/**
 * Código que identifica cada causa de rechazo (nota U05-H-07 de BR-APP-66): los del nodo son sus
 * resultados de sesión y los de la plataforma, los del mapa cerrado de errores. Va como atributo
 * de datos, nunca como texto visible.
 */
const REJECTION_CODES: Readonly<Record<string, string>> = {
  'live_view.rejection.limit': 'rejected_limit',
  'live_view.rejection.mode': 'rejected_mode',
  'live_view.rejection.reused': 'rejected_reused',
  'live_view.rejection.expired': 'rejected_expired',
  'live_view.rejection.zone_without_node': 'zone_without_node',
  'live_view.rejection.rate_limited': 'rate_limited',
};
/** Espera que impone el rechazo `rate_limited` de la plataforma. */
const RETRY_AFTER_SECONDS = 30;

afterEach(() => {
  installTransport(null);
  localStorage.clear();
  sessionStorage.clear();
});

function part(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(`[data-part="${name}"]`);
  if (element === null) throw new Error(`Falta [data-part="${name}"] en el lanzador.`);
  return element;
}

function launcher(): HTMLElement {
  return screen.getByRole('region', { name: text('live_view.region_label') });
}

function requestButton(): HTMLButtonElement {
  const button = launcher().querySelector('[data-part="request"]');
  if (!(button instanceof HTMLButtonElement)) throw new Error('Falta el botón de abrir.');
  return button;
}

function countdown(): string | null {
  const element = launcher().querySelector('[data-part="countdown"]');
  return element?.textContent ?? null;
}

function secondsText(seconds: number): string {
  return `${text('live_view.countdown.link')} ${String(seconds)} ${text('live_view.countdown.seconds')}`;
}

function renderLauncher(harness: LiveViewHarness, onOpened?: (opened: LiveViewOpened) => void) {
  return render(
    <main>
      <LiveViewLauncher
        zone_id={ZONE_ID}
        clock={harness.clock}
        {...(onOpened === undefined ? {} : { onOpened })}
      />
    </main>,
  );
}

/** Pide el token y espera a que el enlace esté en el DOM. */
async function requestToken(): Promise<HTMLAnchorElement> {
  fireEvent.click(requestButton());
  return screen.findByRole<HTMLAnchorElement>('link', { name: text('live_view.link') });
}

/** Todas las claves del catálogo que la pantalla muestra con `data-text-key`. */
function shownKeys(container: HTMLElement): string[] {
  return [...container.querySelectorAll('[data-text-key]')].map(
    (element) => element.getAttribute('data-text-key') ?? '',
  );
}

describe('pantalla previa (BR-APP-66 y nota U05-H-07)', () => {
  it('muestra las cuatro causas del nodo y el aviso del certificado como claves del catálogo', () => {
    const harness = liveViewHarness();
    const { container } = renderLauncher(harness);
    const keys = shownKeys(container);
    for (const key of [...NODE_REJECTION_KEYS, ...CERTIFICATE_KEYS]) {
      expect(Object.hasOwn(catalog, key)).toBe(true);
      expect(keys).toContain(key);
      const element = container.querySelector(`[data-text-key="${key}"]`);
      expect(element?.textContent).toBe(catalog[key as keyof typeof catalog]);
    }
    expect(NODE_REJECTION_KEYS).toHaveLength(4);
  });

  it('explica la red local, el registro del acceso, el difuminado, el agua y qué hacer si no carga', () => {
    const { container } = renderLauncher(liveViewHarness());
    for (const key of [
      'live_view.local_network',
      'live_view.access_recorded',
      'live_view.blurred',
      'live_view.if_not_loading',
      'live_view.watermark',
      'live_view.open_again_token',
    ]) {
      const element = container.querySelector(`[data-text-key="${key}"]`);
      expect(element?.textContent).toBe(catalog[key as keyof typeof catalog]);
    }
  });

  it('explica zone_without_node y rate_limited con su etiqueta', () => {
    const { container } = renderLauncher(liveViewHarness());
    expect(
      container.querySelector('[data-text-key="live_view.rejection.zone_without_node"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-text-key="live_view.rejection.rate_limited"]'),
    ).not.toBeNull();
  });

  it('identifica cada causa con su código como atributo de datos, nunca como texto visible', () => {
    const { container } = renderLauncher(liveViewHarness());
    const items = [...container.querySelectorAll('[data-part="rejections"] li')];
    expect(items).toHaveLength(Object.keys(REJECTION_CODES).length);
    for (const item of items) {
      const key = item.getAttribute('data-text-key') ?? '';
      const code = REJECTION_CODES[key];
      expect(code).toBeDefined();
      expect(item.getAttribute('data-rejection-code')).toBe(code);
      // La etiqueta visible es la del catálogo y no repite el valor crudo.
      expect(item.textContent).toBe(catalog[key as keyof typeof catalog]);
      expect(item.textContent).not.toContain(code ?? '');
    }
    // El modo de la zona se presenta en español, no con el valor del contrato.
    expect(catalog['live_view.rejection.mode']).not.toContain('no_capture');
  });

  it('empieza sin enlace y con el botón de abrir', () => {
    renderLauncher(liveViewHarness());
    expect(launcher().querySelector('[data-part="link"]')).toBeNull();
    expect(requestButton().textContent).toBe(text('live_view.open'));
    expect(requestButton().disabled).toBe(false);
  });
});

describe('sin dirección anunciada (pendiente nº 31, BR-APP-65)', () => {
  it('sin live_view_local_url no hay enlace y se explica que el nodo no la anunció', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff({ live_view_local_url: null })));
    const { container } = renderLauncher(harness);
    fireEvent.click(requestButton());
    await waitFor(() => {
      expect(container.querySelector('[data-part="no_link"]')).not.toBeNull();
    });
    const blocked = part(container, 'no_link');
    expect(blocked.getAttribute('data-text-key')).toBe('live_view.pending_local_url');
    expect(blocked.getAttribute('data-problem')).toBe('no_local_url');
    expect(blocked.textContent).toBe(text('live_view.pending_local_url'));
    expect(container.querySelector('[data-part="link"]')).toBeNull();
    expect(tokenFoundIn(TOKEN)).toEqual([]);
  });

  it('una dirección que no es utilizable tampoco produce enlace', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff({ live_view_local_url: 'http://nodo.local:8443/' })));
    const { container } = renderLauncher(harness);
    fireEvent.click(requestButton());
    await waitFor(() => {
      expect(container.querySelector('[data-part="no_link"]')).not.toBeNull();
    });
    const blocked = part(container, 'no_link');
    expect(blocked.getAttribute('data-text-key')).toBe('live_view.unusable_url');
    expect(blocked.getAttribute('data-problem')).toBe('unusable');
    expect(container.querySelector('[data-part="link"]')).toBeNull();
  });
});

describe('enlace con el token en el fragmento (BR-APP-65, PR-APP-20)', () => {
  it('apunta al nodo con el token en el fragmento, sin consulta y con noopener noreferrer', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff()));
    renderLauncher(harness);
    const link = await requestToken();
    expect(link.getAttribute('href')).toBe(`${LOCAL_URL}#token=${TOKEN}`);
    expect(link.target).toBe('_blank');
    expect(link.rel).toBe('noopener noreferrer');
    expect(new URL(link.href).search).toBe('');
    expect(countdown()).toBe(secondsText(LIVE_VIEW_TOKEN_SECONDS));
    // Mientras el enlace vive, el botón no se ofrece: el enlace es el único camino (D-1).
    expect(launcher().querySelector('[data-part="request"]')).toBeNull();
  });

  it('el enlace se elimina del DOM exactamente a los 600 s', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff()));
    renderLauncher(harness);
    const link = await requestToken();
    expect(link).toBeDefined();

    act(() => {
      harness.clock.advance((LIVE_VIEW_TOKEN_SECONDS - 1) * 1000);
    });
    expect(countdown()).toBe(secondsText(1));
    expect(launcher().querySelector('[data-part="link"]')).not.toBeNull();

    act(() => {
      harness.clock.advance(999);
    });
    expect(launcher().querySelector('[data-part="link"]')).not.toBeNull();

    act(() => {
      harness.clock.advance(1);
    });
    expect(launcher().querySelector('[data-part="link"]')).toBeNull();
    expect(launcher().querySelector('[data-part="countdown"]')).toBeNull();
    expect(tokenFoundIn(TOKEN)).toEqual([]);
    expect(requestButton().textContent).toBe(text('live_view.open_again'));
    expect(requestButton().disabled).toBe(false);
  });

  it('al pulsarlo se elimina del DOM, se descarta el token y se avisa sin él', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff()));
    const opened = vi.fn<(opened: LiveViewOpened) => void>();
    renderLauncher(harness, opened);
    const link = await requestToken();
    fireEvent.click(link);

    expect(launcher().querySelector('[data-part="link"]')).toBeNull();
    expect(opened).toHaveBeenCalledExactlyOnceWith({
      zone_id: ZONE_ID,
      opened_at_ms: harness.clock.now(),
    });
    expect(JSON.stringify(opened.mock.calls)).not.toContain(TOKEN);
    expect(tokenFoundIn(TOKEN)).toEqual([]);
    expect(requestButton().textContent).toBe(text('live_view.open_again'));
  });

  it('el botón central del ratón también elimina el enlace del DOM', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff()));
    const opened = vi.fn<(opened: LiveViewOpened) => void>();
    renderLauncher(harness, opened);
    const link = await requestToken();

    // El botón derecho no abre nada: el enlace sigue donde estaba y nadie recibe el acuse.
    auxClick(link, 2);
    expect(launcher().querySelector('[data-part="link"]')).not.toBeNull();
    expect(opened).not.toHaveBeenCalled();

    // El botón central abre la pestaña sin disparar `click`; el enlace sale igualmente del DOM.
    auxClick(link, 1);
    expect(launcher().querySelector('[data-part="link"]')).toBeNull();
    expect(opened).toHaveBeenCalledExactlyOnceWith({
      zone_id: ZONE_ID,
      opened_at_ms: harness.clock.now(),
    });
    expect(tokenFoundIn(TOKEN)).toEqual([]);
    expect(requestButton().textContent).toBe(text('live_view.open_again'));
  });

  it('nunca llama a window.open: la pestaña nueva la abre el enlace', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff()));
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    renderLauncher(harness);
    const link = await requestToken();
    expect(open).not.toHaveBeenCalled();
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    fireEvent.click(link);
    expect(open).not.toHaveBeenCalled();
    expect(launcher().querySelector('[data-part="link"]')).toBeNull();
  });
});

describe('rechazos de la plataforma (BR-APP-66, BR-APP-71)', () => {
  it('rate_limited deshabilita «abrir de nuevo» exactamente retry_after_seconds', async () => {
    const harness = liveViewHarness();
    server.use(harness.rejected('rate_limited', 429, { retryAfterSeconds: RETRY_AFTER_SECONDS }));
    renderLauncher(harness);
    fireEvent.click(requestButton());
    await waitFor(() => {
      expect(launcher().querySelector('[data-text-key="errors.countdown"]')).not.toBeNull();
    });

    expect(requestButton().textContent).toBe(text('live_view.open_again'));
    expect(requestButton().disabled).toBe(true);
    expect(part(launcher(), 'wait').textContent).toBe(
      `${text('live_view.countdown.wait')} ${String(RETRY_AFTER_SECONDS)} ${text('live_view.countdown.seconds')}`,
    );

    act(() => {
      harness.clock.advance(RETRY_AFTER_SECONDS * 1000 - 1);
    });
    expect(requestButton().disabled).toBe(true);

    act(() => {
      harness.clock.advance(1);
    });
    expect(requestButton().disabled).toBe(false);
    expect(launcher().querySelector('[data-part="wait"]')).toBeNull();
  });

  it('zone_without_node se muestra con la etiqueta del mapa cerrado y deja pedir otro enlace', async () => {
    const harness = liveViewHarness();
    server.use(harness.rejected('zone_without_node', 409));
    const { container } = renderLauncher(harness);
    fireEvent.click(requestButton());
    await waitFor(() => {
      expect(container.querySelector('[data-text-key="errors.zone_without_node"]')).not.toBeNull();
    });
    expect(container.querySelector('[data-part="message"]')?.textContent).toBe(
      text('errors.zone_without_node'),
    );
    expect(container.querySelector('[data-part="link"]')).toBeNull();
    expect(requestButton().disabled).toBe(false);
  });

  it('un rechazo sin retry_after_seconds no impone espera propia', async () => {
    const harness = liveViewHarness();
    server.use(harness.rejected('temporarily_unavailable', 503));
    renderLauncher(harness);
    fireEvent.click(requestButton());
    await waitFor(() => {
      expect(launcher().querySelector('[data-text-key="errors.transient"]')).not.toBeNull();
    });
    expect(requestButton().textContent).toBe(text('live_view.open_again'));
    expect(requestButton().disabled).toBe(false);
    expect(launcher().querySelector('[data-part="wait"]')).toBeNull();
  });

  it('un error del servidor no deja el token en la URL ni en el historial', async () => {
    const harness = liveViewHarness();
    server.use(harness.rejected('internal_error', 500));
    const before = window.location.href;
    renderLauncher(harness);
    fireEvent.click(requestButton());
    await waitFor(() => {
      expect(launcher().querySelector('[data-text-key="errors.internal_error"]')).not.toBeNull();
    });
    expect(window.location.href).toBe(before);
    expect(tokenFoundIn(TOKEN)).toEqual([]);
  });
});

describe('accesibilidad (NFR-APP-50)', () => {
  it('sin violaciones serias en la pantalla previa y con el enlace vivo', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff()));
    const { container } = renderLauncher(harness);
    await requestToken();
    const results = await axe(container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });
});

describe('entrega del token', () => {
  it('no viaja en la consulta, la cabecera ni el cuerpo de la petición', async () => {
    const harness = liveViewHarness();
    server.use(harness.granted(handoff({ expires_at: EXPIRES_AT })));
    renderLauncher(harness);
    await requestToken();
    expect(harness.requests).toHaveLength(1);
    const [sent] = harness.requests;
    if (sent === undefined) throw new Error('falta la petición del token');
    expect(sent.url).not.toContain(TOKEN);
    expect(new URL(sent.url).search).toBe('');
    expect(JSON.stringify(sent.init ?? {})).not.toContain(TOKEN);
    expect(JSON.stringify(harness.devLog)).not.toContain(TOKEN);
    expect(harness.devLog.map((entry) => entry.route)).toEqual([
      '/zones/{zone_id}/live-view-token',
    ]);
  });
});

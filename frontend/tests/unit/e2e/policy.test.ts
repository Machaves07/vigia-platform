import { EventEmitter } from 'node:events';

import type { BrowserContext } from '@playwright/test';
import { describe, expect, it, vi } from 'vitest';

import { policySummary, policyTotals, urlProblem, watchPolicy } from '../../e2e/policy';

async function harness({
  alreadyOpen = false,
  origin,
}: { alreadyOpen?: boolean; origin?: string } = {}) {
  const disposable = { dispose: vi.fn(), [Symbol.asyncDispose]: vi.fn() };
  const mainFrame = { url: () => `${origin ?? ''}/` };
  const page = Object.assign(new EventEmitter(), { mainFrame: () => mainFrame });
  const context = Object.assign(new EventEmitter(), {
    exposeBinding: vi.fn<BrowserContext['exposeBinding']>().mockResolvedValue(disposable),
    addInitScript: vi.fn<BrowserContext['addInitScript']>().mockResolvedValue(disposable),
    pages: vi.fn(() => (alreadyOpen ? [page] : [])),
  });
  const check = await watchPolicy(context as unknown as BrowserContext, '', origin);
  if (!alreadyOpen) context.emit('page', page);
  return { context, page, check, mainFrame };
}

describe('guarda de CSP y tipos confiables del arnés', () => {
  it.each([
    'Refused to apply inline style because it violates Content Security Policy',
    'Content-Security-Policy: The page settings blocked an inline style (style-src-attr)',
    "This document requires 'TrustedHTML' assignment",
  ])('rechaza el diagnóstico del navegador: %s', async (diagnostic) => {
    const { page, check } = await harness();
    page.emit('console', { text: () => diagnostic });
    expect(check).toThrow(/Violaciones de CSP o Trusted Types/);
  });

  it('vigila también la página que ya estaba abierta al instalar la guarda', async () => {
    const { page, check } = await harness({ alreadyOpen: true });
    page.emit('console', { text: () => "This document requires 'TrustedHTML' assignment" });
    expect(check).toThrow(/Violaciones de CSP o Trusted Types/);
  });

  it('rechaza también un error de un sumidero de tipos confiables', async () => {
    const { page, check } = await harness();
    page.emit('pageerror', new Error('TrustedHTML assignment required'));
    expect(check).toThrow(/Violaciones de CSP o Trusted Types/);
  });

  it('no confunde un aviso de directiva no soportada con una violación', async () => {
    const { page, check, context } = await harness();
    page.emit('console', {
      text: () => "Unrecognized Content-Security-Policy directive 'require-trusted-types-for'",
    });
    expect(check).not.toThrow();
    expect(context.exposeBinding).toHaveBeenCalledOnce();
    expect(context.addInitScript).toHaveBeenCalledOnce();
  });
});

const ORIGIN = 'https://127.0.0.1:8443';
const POLICY =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; " +
  "frame-ancestors 'none'; require-trusted-types-for 'script'";

function documentResponse(url: string, policy: string | null) {
  return {
    url: () => url,
    request: () => ({ resourceType: () => 'document' }),
    headers: () => (policy === null ? {} : { 'content-security-policy': policy }),
  };
}

describe('agregado del arnés y datos en el cliente (VIG-336)', () => {
  it('registra la política de tipos confiables que intente crear la página', async () => {
    const { context, check } = await harness({ origin: ORIGIN });
    const binding = context.exposeBinding.mock.calls[0];
    expect(binding?.[0]).toBe('__vigiaPolicyViolation');
    const callback = binding?.[1] as (source: unknown, directive: string) => void;
    const createPolicy = vi.fn(() => ({}));
    const page = globalThis as unknown as Record<string, unknown>;
    page.trustedTypes = { createPolicy };
    page.__vigiaPolicyViolation = (directive: string) => {
      callback(null, directive);
      return Promise.resolve();
    };
    try {
      const script = context.addInitScript.mock.calls[0]?.[0] as () => void;
      script();
      (page.trustedTypes as { createPolicy: (name: string) => unknown }).createPolicy('x');
    } finally {
      delete page.trustedTypes;
      delete page.__vigiaPolicyViolation;
    }
    expect(createPolicy).toHaveBeenCalledOnce();
    expect(check).toThrow(/Violaciones de CSP o Trusted Types/);
  });

  it('exige la política de producción en los documentos del propio origen', async () => {
    const good = await harness({ origin: ORIGIN });
    good.page.emit('response', documentResponse(`${ORIGIN}/home`, POLICY));
    good.page.emit('response', documentResponse('https://deposito.invalid/x', null));
    expect(good.check).not.toThrow();

    const bad = await harness({ origin: ORIGIN });
    bad.page.emit('response', documentResponse(`${ORIGIN}/home`, "default-src 'self'"));
    expect(bad.check).toThrow(/Violaciones de CSP o Trusted Types/);
  });

  it('rechaza la consola de los guiones de la aplicación y no la del navegador', async () => {
    const browser = await harness({ origin: ORIGIN });
    browser.page.emit('console', {
      text: () => 'Failed to load resource: the server responded with a status of 404',
      location: () => ({ url: `${ORIGIN}/findings/x` }),
    });
    expect(browser.check).not.toThrow();

    const app = await harness({ origin: ORIGIN });
    app.page.emit('console', {
      text: () => 'cualquier cosa',
      location: () => ({ url: `${ORIGIN}/assets/index-abc.js` }),
    });
    expect(app.check).toThrow(/Datos en el cliente/);
  });

  it.each([
    ['/findings/0190a1b2-2222-7000-8000-000000000001', false],
    ['/zones/0190a1b2-1111-7000-8000-000000000001/monthly/2026-09', false],
    ['/account#sessions', false],
    ['/findings?zona=L%C3%ADnea%203', true],
    ['/findings/Juan%20P%C3%A9rez', true],
  ] as const)('navegación a %s: rechazada %s', async (path, rejected) => {
    const { page, check, mainFrame } = await harness({ origin: ORIGIN });
    mainFrame.url = () => `${ORIGIN}${path}`;
    page.emit('framenavigated', mainFrame);
    if (rejected) expect(check).toThrow(/Datos en el cliente/);
    else expect(check).not.toThrow();
  });

  it('admite el token de invitación solo tras /invitations', () => {
    const token = 'Sint_'.repeat(9);
    expect(urlProblem(new URL(`${ORIGIN}/invitations/${token}`))).toBeNull();
    expect(urlProblem(new URL(`${ORIGIN}/findings/${token}`))).toBe('texto libre en la ruta');
  });

  it('el resumen agrega los documentos con la política de producción', async () => {
    const before = policyTotals();
    const { page, check } = await harness({ origin: ORIGIN });
    page.emit('response', documentResponse(`${ORIGIN}/login`, POLICY));
    expect(check).not.toThrow();
    expect(policyTotals().documents).toBe(before.documents + 1);
    expect(policySummary()).toMatch(/^\[política\] contextos \d+, páginas \d+, documentos/);
  });
});

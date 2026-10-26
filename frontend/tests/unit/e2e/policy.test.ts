import { EventEmitter } from 'node:events';

import type { BrowserContext } from '@playwright/test';
import { describe, expect, it, vi } from 'vitest';

import { watchPolicy } from '../../e2e/policy';

async function harness({ alreadyOpen = false } = {}) {
  const disposable = { dispose: vi.fn(), [Symbol.asyncDispose]: vi.fn() };
  const page = new EventEmitter();
  const context = Object.assign(new EventEmitter(), {
    exposeBinding: vi.fn<BrowserContext['exposeBinding']>().mockResolvedValue(disposable),
    addInitScript: vi.fn<BrowserContext['addInitScript']>().mockResolvedValue(disposable),
    pages: vi.fn(() => (alreadyOpen ? [page] : [])),
  });
  const check = await watchPolicy(context as unknown as BrowserContext);
  if (!alreadyOpen) context.emit('page', page);
  return { context, page, check };
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

/**
 * Sección estática «navegador no compatible» (LC-APP-01, PAT-APP-REN-01; NFR-APP-31; criterios 2 y
 * 3 de VIG-289): la sección de `index.html` está oculta al cargar, tiene `lang="es"`, su texto es el
 * del catálogo y pasa `axe`; el arranque la deja visible —y **no** carga React— cuando falta alguna
 * capacidad. Se comparan claves y textos exactos del catálogo, nunca subcadenas.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { bootstrapPage, probeWith } from '../../support/capabilities';
import {
  CAPABILITIES,
  revealUnsupported,
  start,
  UNSUPPORTED_SECTION_ID,
  type CapabilityId,
} from '@/bootstrap/check';
import { text } from '@/texts';

/** La página de `index.html` montada en el documento de jsdom (axe necesita ese documento). */
function mountIndexHtml(): Element {
  const { document: page } = bootstrapPage();
  document.documentElement.lang = 'es';
  document.body.replaceChildren(
    ...[...page.body.childNodes].map((node) => document.importNode(node, true)),
  );
  const section = document.getElementById(UNSUPPORTED_SECTION_ID);
  if (section === null) throw new Error(`index.html: falta la sección #${UNSUPPORTED_SECTION_ID}.`);
  return section;
}

function withoutCapability(missing: CapabilityId): readonly CapabilityId[] {
  return CAPABILITIES.filter((capability) => capability !== missing);
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('sección de navegador no compatible', () => {
  it('está oculta al cargar, con lang="es" y el texto del catálogo', () => {
    const section = mountIndexHtml();
    expect(section.getAttribute('lang')).toBe('es');
    expect(section.hasAttribute('hidden')).toBe(true);
    expect(section.querySelector('h1')?.textContent).toBe(text('bootstrap.unsupported_title'));
    expect(
      [...section.querySelectorAll('p')].map((paragraph) => normalize(paragraph.textContent)),
    ).toEqual([text('bootstrap.unsupported_body')]);
  });

  it('el arranque la deja visible y no carga React cuando falta una capacidad', () => {
    for (const missing of CAPABILITIES) {
      const section = mountIndexHtml();
      const loadApp = vi.fn(() => Promise.resolve(undefined));
      const pending = start({
        probe: probeWith(withoutCapability(missing)),
        document,
        loadApp,
      });
      expect(pending, `falta ${missing}`).toBeNull();
      expect(loadApp, `falta ${missing}`).not.toHaveBeenCalled();
      expect(section.hasAttribute('hidden'), `falta ${missing}`).toBe(false);
    }
  });

  it('con todas las capacidades la deja oculta y carga el paquete inicial una sola vez', async () => {
    const section = mountIndexHtml();
    const loadApp = vi.fn(() => Promise.resolve(undefined));
    const pending = start({ probe: probeWith([...CAPABILITIES]), document, loadApp });
    await pending;
    expect(loadApp).toHaveBeenCalledTimes(1);
    expect(section.hasAttribute('hidden')).toBe(true);
  });

  it('pasa axe una vez visible', async () => {
    mountIndexHtml();
    revealUnsupported(document);
    const results = await axe(document.body);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  }, 30_000);

  it('sin la sección en el documento no hay nada que mostrar y React tampoco se carga', () => {
    document.body.replaceChildren();
    expect(revealUnsupported(document)).toBe(false);
    const loadApp = vi.fn(() => Promise.resolve(undefined));
    expect(start({ probe: probeWith([]), document, loadApp })).toBeNull();
    expect(loadApp).not.toHaveBeenCalled();
  });
});

function normalize(textContent: string | null): string {
  return (textContent ?? '').replace(/\s+/g, ' ').trim();
}

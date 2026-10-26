/**
 * NFR-APP-32 y PAT-APP-SEG-07 (BR-APP-19): ningún dato personal ni texto libre en la URL, el
 * título de la pestaña ni el almacenamiento del navegador.
 *
 * - Rutas declaradas: cada segmento de cada ruta de `SCREENS` es una palabra fija en minúsculas o
 *   un parámetro de la lista cerrada (identificadores UUID, el mes `AAAA-MM` y el token opaco de
 *   la invitación); ninguna lleva consulta ni fragmento.
 * - Enlaces internos: con cada pantalla pintada con los datos de MSW, todo enlace del propio
 *   origen apunta a una ruta declarada con parámetros de esa forma, sin consulta y como mucho con
 *   el ancla de un elemento de la página (`#sessions`).
 * - Título: ningún archivo de `src/` escribe `document.title` ni pinta `<title>`; el de
 *   `index.html` es «Vigía» y montar cualquier pantalla no lo cambia.
 * - Almacenamiento: tras montar cada pantalla solo existe la clave de `UiPreferences` en
 *   `localStorage` y `sessionStorage` está vacío.
 *
 * La consola de la construcción de producción la cubren `tests/unit/build/no_console.test.ts` y
 * los recorridos de extremo a extremo (`tests/e2e/fixtures.ts`).
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { SCREENS } from '@/app/screens';
import { PREFERENCES_KEY } from '@/state/scope';

import { mountEntry, SCREEN_ENTRIES } from '../support/screenTour';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** Forma de cada parámetro de ruta admitido; cualquier otro nombre es texto libre posible. */
const PARAM_FORMS: Readonly<Record<string, RegExp>> = {
  zone_id: UUID,
  plant_id: UUID,
  finding_state_id: UUID,
  node_id: UUID,
  record_id: UUID,
  period: /^\d{4}-(?:0[1-9]|1[0-2])$/,
  // Token opaco del enlace de invitación (BR-APP-18): base64url, sin datos de la persona.
  token: /^[A-Za-z0-9_-]{16,512}$/,
};
const STATIC_SEGMENT = /^[a-z][a-z0-9-]*$/;

const TEMPLATES: readonly string[] = SCREENS.flatMap((screen) =>
  screen.route_groups.flatMap((group) => [...group.paths]),
);

/** ¿`path` encaja en alguna ruta declarada con parámetros de la forma admitida? */
function declared(path: string): boolean {
  const segments = path.split('/').slice(1);
  return TEMPLATES.some((template) => {
    const expected = template.split('/').slice(1);
    return (
      expected.length === segments.length &&
      expected.every((part, index) => {
        const segment = segments[index] ?? '';
        if (!part.startsWith(':')) return part === segment;
        return PARAM_FORMS[part.slice(1)]?.test(decodeURIComponent(segment)) === true;
      })
    );
  });
}

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? sources(join(directory, entry.name))
      : /\.(?:ts|tsx)$/.test(entry.name)
        ? [join(directory, entry.name)]
        : [],
  );
}

describe('NFR-APP-32: rutas declaradas', () => {
  it.each(TEMPLATES)('%s solo tiene segmentos fijos y parámetros de la lista cerrada', (path) => {
    expect(path).not.toMatch(/[?#]/);
    for (const segment of path.split('/').slice(1)) {
      if (segment.startsWith(':')) expect(Object.keys(PARAM_FORMS)).toContain(segment.slice(1));
      else expect(segment).toMatch(STATIC_SEGMENT);
    }
  });

  it('la comprobación rechaza texto libre, consultas y parámetros mal formados', () => {
    expect(declared('/findings/0190a1b2-2222-7000-8000-000000000001')).toBe(true);
    expect(declared('/findings/Juan%20P%C3%A9rez')).toBe(false);
    expect(declared('/zones/0190a1b2-1111-7000-8000-000000000001/monthly/septiembre')).toBe(false);
    expect(declared('/otra/ruta')).toBe(false);
  });
});

describe('NFR-APP-32: título de la pestaña', () => {
  it('index.html titula «Vigía» y ningún archivo de src/ cambia el título', () => {
    const index = readFileSync(join(ROOT, 'index.html'), 'utf8');
    expect(/<title>([^<]*)<\/title>/.exec(index)?.[1]).toBe('Vigía');
    const writers = sources(join(ROOT, 'src')).filter((file) =>
      /document\s*\.\s*title|<title[\s>]/.test(readFileSync(file, 'utf8')),
    );
    expect(writers).toEqual([]);
  });
});

/** Enlaces internos con identificador comprobados: la prueba no puede pasar sin enlaces. */
let withParameters = 0;

describe(
  'NFR-APP-32: enlaces, título y almacenamiento con cada pantalla',
  { timeout: 60_000 },
  () => {
    it.each(SCREEN_ENTRIES)('$screen · $group ($template)', async (entry) => {
      document.title = 'Vigía';
      await mountEntry(entry);
      expect(document.title).toBe('Vigía');

      const links = [...document.querySelectorAll<HTMLAnchorElement>('a[href]')]
        .map((anchor) => new URL(anchor.getAttribute('href') ?? '', location.href))
        .filter((url) => url.origin === location.origin);
      for (const url of links) {
        expect(url.search, url.href).toBe('');
        // Solo anclas a un elemento de la propia página (`#sessions`), nunca un dato.
        expect(url.hash, url.href).toMatch(/^(?:#[A-Za-z0-9_-]{1,80})?$/);
        if (url.pathname === '/') continue;
        expect(declared(url.pathname), url.pathname).toBe(true);
        if (/\/[0-9a-f]{8}-/.test(url.pathname)) withParameters += 1;
      }

      for (let index = 0; index < localStorage.length; index += 1) {
        expect(localStorage.key(index)).toBe(PREFERENCES_KEY);
      }
      expect(sessionStorage.length).toBe(0);
    });

    it('el recorrido comprobó enlaces internos con identificadores', () => {
      expect(withParameters).toBeGreaterThan(0);
    });
  },
);

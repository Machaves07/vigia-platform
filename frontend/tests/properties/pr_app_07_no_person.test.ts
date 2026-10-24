/**
 * PR-APP-07 (BR-APP-21, 22; NFR-APP-45, bloqueante): para todo dato de vista de lectura generado
 * con campos extra inyectados, el árbol renderizado de cada vista registrada en `tests/views/` no
 * contiene ningún campo fuera de `allowedFields`, ningún valor de identificador de persona
 * (persona, seguimiento, rostro, trabajador, documento, correo o teléfono) ni «personas» seguido
 * de número. Corre también sobre la vista de autoprueba y sobre sus mutaciones, que deben caer.
 */
import { render } from '@testing-library/react';
import { createElement, Fragment } from 'react';
import { createPortal } from 'react-dom';
import { describe, expect, it } from 'vitest';

import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';
import {
  assertNoPersonData,
  forbiddenRendered,
  personFieldsDeclared,
} from '../support/viewProperties';
import { views } from '../views';
import { renderSelftest, selftestView } from '../views/_selftest';
import type { ViewRegistration } from '../views/types';

const registered = Object.entries(views);
const count = `${String(registered.length)} vistas`;

describe('PR-APP-07: ninguna pantalla pinta campos fuera de su vista ni datos de persona', () => {
  it(
    `vistas registradas en tests/views (${count})`,
    () => {
      process.stdout.write(`PR-APP-07: ${count}\n`);
      for (const [name, registration] of registered) {
        expect(personFieldsDeclared(registration), name).toEqual([]);
        assertNoPersonData(registration);
      }
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'vista de autoprueba',
    () => {
      expect(personFieldsDeclared(selftestView)).toEqual([]);
      assertNoPersonData(selftestView);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cae en rojo nombrando vista y campo si la vista pinta un campo inyectado',
    () => {
      const allowed = new Set(selftestView.allowedFields);
      const leaky: ViewRegistration = {
        ...selftestView,
        render: (data, context) =>
          createElement(
            Fragment,
            null,
            renderSelftest(data, context),
            ...Object.entries(data)
              .filter(([key]) => !allowed.has(key))
              .map(([key, value]) => createElement('p', { key }, String(value))),
          ),
      };
      expect(() => {
        assertNoPersonData(leaky);
      }).toThrow(/RV-00 \(SCR-00\): campo «[a-z][a-z0-9_]*» fuera de allowedFields aparece en/);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cae en rojo nombrando el campo anidado si la vista pinta los campos inyectados de cada registro',
    () => {
      const shown = new Set(['item_id', 'organization_id', 'title', 'status']);
      const leaky: ViewRegistration = {
        ...selftestView,
        render: (data, context) =>
          createElement(
            Fragment,
            null,
            renderSelftest(data, context),
            createElement(
              'ul',
              null,
              ...(Array.isArray(data.items) ? (data.items as Record<string, unknown>[]) : []).map(
                (entry, index) =>
                  createElement('li', {
                    key: index,
                    title: Object.entries(entry)
                      .filter(([key]) => !shown.has(key))
                      .map(([, value]) => String(value))
                      .join(' '),
                  }),
              ),
            ),
          ),
      };
      expect(() => {
        assertNoPersonData(leaky);
      }).toThrow(
        /RV-00 \(SCR-00\): campo «items\[\d+\]\.[a-z][a-z0-9_]*» fuera de allowedFields aparece en el atributo title de <li>/,
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cae en rojo si la vista pinta un campo inyectado en un portal fuera de su contenedor',
    () => {
      const allowed = new Set(selftestView.allowedFields);
      const leaky: ViewRegistration = {
        ...selftestView,
        render: (data, context) =>
          createElement(
            Fragment,
            null,
            renderSelftest(data, context),
            createPortal(
              createElement(
                'p',
                null,
                Object.entries(data)
                  .filter(([key]) => !allowed.has(key))
                  .map(([, value]) => String(value))
                  .join(' '),
              ),
              document.body,
            ),
          ),
      };
      expect(() => {
        assertNoPersonData(leaky);
      }).toThrow(/RV-00 \(SCR-00\): campo «[a-z][a-z0-9_]*» fuera de allowedFields aparece en/);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('una vista que declara un campo de persona en allowedFields no se admite', () => {
    expect(
      personFieldsDeclared({ ...selftestView, allowedFields: ['title', 'track_id', 'Correo'] }),
    ).toEqual(['track_id', 'Correo']);
  });
});

describe('PR-APP-07: inspección de valores renderizados, no de textos fijos', () => {
  const registration = { view: 'RV-00', screen: 'SCR-00', allowedFields: ['zone_code'] } as const;
  const data = {
    zone_code: 'Z-PLANTA01',
    email: 'pedro4521@planta.example',
    track_id: 'trk-88412093',
  };

  function violations(node: ReturnType<typeof createElement>): string[] {
    const { baseElement, unmount } = render(node);
    try {
      return forbiddenRendered(registration, data, baseElement);
    } finally {
      unmount();
    }
  }

  it('detecta el valor inyectado escrito con homoglifos y espacios invisibles', () => {
    // «р», «а», «е» y «о» cirílicas; U+200B, U+2060 y U+00A0 entre caracteres.
    const disguised = '\u0440\u200B\u0435dr\u043E4521 @ \u0440l\u2060\u0430nt\u0430.\u00A0example';
    expect(violations(createElement('span', null, disguised))).toEqual([
      'RV-00 (SCR-00): campo «email» fuera de allowedFields aparece en el texto.',
    ]);
    // Ancho completo (NFKC) y guion que no corta (U+2011).
    const fullWidth = '\uFF34\uFF32\uFF2B\u2011\uFF18\uFF18\uFF14\uFF11\uFF12\uFF10\uFF19\uFF13';
    expect(violations(createElement('span', { title: fullWidth }, 'Zona'))).toEqual([
      'RV-00 (SCR-00): campo «track_id» fuera de allowedFields aparece en el atributo title de <span>.',
    ]);
    expect(
      violations(createElement('span', null, createElement('b', null, 'trk-8841'), '2093')),
    ).toEqual(['RV-00 (SCR-00): campo «track_id» fuera de allowedFields aparece en el texto.']);
  });

  it('no marca textos fijos que solo se parecen a un campo de persona', () => {
    expect(
      violations(
        createElement(
          'p',
          { 'data-kind': 'person_id track_id face' },
          'Persona responsable · Trabajador · Correo · Z-PLANTA01',
        ),
      ),
    ).toEqual([]);
  });

  it('detecta el valor inyectado en el valor de un campo de formulario', () => {
    const { baseElement, getByRole, unmount } = render(
      createElement('input', { 'aria-label': 'Contacto' }),
    );
    try {
      // Solo la propiedad `value`, no el atributo: lo que vería quien usa la pantalla.
      (getByRole('textbox') as HTMLInputElement).value = data.email;
      expect(forbiddenRendered(registration, data, baseElement)).toEqual([
        'RV-00 (SCR-00): campo «email» fuera de allowedFields aparece en el valor de <input>.',
      ]);
    } finally {
      unmount();
    }
  });

  it('no marca «persona» de un rótulo junto a un número de otro nodo', () => {
    expect(
      violations(
        createElement(
          'section',
          null,
          createElement('h3', null, 'Zona 3'),
          createElement(
            'dl',
            null,
            createElement('dt', null, 'Persona responsable'),
            createElement('dd', null, 'Z-PLANTA01'),
            createElement('dt', null, 'Firma de la persona'),
            createElement('dd', null, '2026-10-24'),
          ),
          createElement(
            'table',
            null,
            createElement(
              'tbody',
              null,
              createElement(
                'tr',
                null,
                createElement('td', null, 'Invitar a una persona'),
                createElement('td', null, '12'),
              ),
            ),
          ),
          createElement('p', null, 'Sesiones activas de la persona'),
          createElement('p', null, '4'),
        ),
      ),
    ).toEqual([]);
  });

  it('detecta «personas» seguido de número aunque lleve homoglifos o invisibles', () => {
    expect(violations(createElement('p', null, '\u0420\u0435rsonas\u200B: 3'))).toEqual([
      'RV-00 (SCR-00): «personas» seguido de número en el texto de <p>.',
    ]);
    expect(violations(createElement('p', null, '\u0420\u200Bersonas:\u00A0#3'))).toEqual([
      'RV-00 (SCR-00): «personas» seguido de número en el texto de <p>.',
    ]);
    expect(violations(createElement('p', null, 'Turno de noche: 12 personas'))).toEqual([
      'RV-00 (SCR-00): «personas» seguido de número en el texto de <p>.',
    ]);
    expect(violations(createElement('p', { 'aria-label': '12 personas' }, 'Zona'))).toEqual([
      'RV-00 (SCR-00): «personas» seguido de número en el atributo aria-label de <p>.',
    ]);
  });
});

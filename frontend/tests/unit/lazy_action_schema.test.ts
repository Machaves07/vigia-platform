import { expect, it, vi } from 'vitest';

const { readLimits } = vi.hoisted(() => ({
  readLimits: vi.fn(() => ({ minLength: 20, maxLength: 2000 })),
}));

vi.mock('@/components/ReasonField/limits', () => ({ reasonLimits: readLimits }));

import { ActionForm } from '@/pages/scr09/loop/ActionForm';

// El perfil limpia los spies antes de cada caso; conservar la fase de importación.
const importCalls = readLimits.mock.calls.length;

it('no consume el esquema compartido antes de renderizar la entrada lazy del lazo', () => {
  // En producción el esquema y la página forman un ciclo entre porciones. Su exportación
  // aún puede estar sin inicializar al evaluar el módulo, pero ya existe al renderizar.
  expect(ActionForm).toBeTypeOf('function');
  expect(importCalls).toBe(0);
});

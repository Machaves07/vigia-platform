/**
 * O-16 (recorrido de cierre M10): el responsable que se muestra de una acción y el de la acción
 * vigente para la tarjeta «Siguiente paso» (`pages/shared/actionState.ts`). Oráculos literales.
 */
import { describe, expect, it } from 'vitest';
import { assigneeLine, assigneeName, currentAssignee } from '@/pages/shared/actionState';
import { text } from '@/texts';

const action = (state: string, name: string | null) => ({ state, assignee_display_name: name });

describe('O-16: el responsable de la acción', () => {
  it('con nombre, «Responsable: <nombre>»; sin nombre o en blanco, «Responsable no disponible»', () => {
    expect(assigneeLine('Persona de prueba (line_manager)')).toBe(
      'Responsable: Persona de prueba (line_manager)',
    );
    expect(assigneeLine(null)).toBe('Responsable no disponible');
    expect(assigneeLine('   ')).toBe('Responsable no disponible');
    expect(assigneeName('Persona de prueba (line_manager)')).toBe(
      'Persona de prueba (line_manager)',
    );
    expect(assigneeName(null)).toBe(text('finding_journey.assignee_unavailable'));
  });

  it('la vigente es la última asignada o vencida, no una sustituida ni la primera', () => {
    expect(
      currentAssignee([
        action('assigned', 'Persona de prueba (A)'),
        action('superseded', 'Persona de prueba (B)'),
        action('overdue', 'Persona de prueba (C)'),
      ]),
    ).toEqual({ name: 'Persona de prueba (C)' });
    // Una reapertura deja la acción anterior `assigned` y asigna otra después.
    expect(
      currentAssignee([
        action('assigned', 'Persona de prueba (A)'),
        action('assigned', 'Persona de prueba (D)'),
      ]),
    ).toEqual({ name: 'Persona de prueba (D)' });
    expect(currentAssignee([action('assigned', null)])).toEqual({ name: null });
    expect(currentAssignee([action('superseded', 'Persona de prueba (B)')])).toBeNull();
    expect(currentAssignee([])).toBeNull();
  });
});

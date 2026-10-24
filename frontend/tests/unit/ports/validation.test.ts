/**
 * `ports.validation` al enviar (LC-APP-16, BR-APP-42 y 64; VIG-280): `validateBody` y
 * `validateQuery` devuelven un problema por campo con la clave del catálogo de textos, con las
 * mismas longitudes, formatos, patrones y obligatoriedad que publicó Pydantic.
 */
import { describe, expect, it } from 'vitest';

import {
  validateBody,
  validateQuery,
  type QueryOperationId,
  type ValidationIssue,
} from '@/ports/validation';
import { texts } from '@/texts';

const LOGIN = 'login_auth_login_post';
const NOTIFICATIONS: QueryOperationId = 'list_notifications_notifications_get';
const EXPOSURE: QueryOperationId = 'get_exposure_accreditation_exposure_get';
const ZONE = '018cc251-f400-7000-8000-000000000001';

function issuesOf(result: ReturnType<typeof validateBody>): readonly ValidationIssue[] {
  return result.ok ? [] : result.issues;
}

describe('validateBody', () => {
  it('acepta un cuerpo del tipo generado', () => {
    expect(validateBody(LOGIN, { email: 'persona@ejemplo.invalid', password: 'x' })).toEqual({
      ok: true,
    });
  });

  it('nombra cada campo con su clave: obligatorio, longitud, tipo y campo fuera del tipo', () => {
    expect(issuesOf(validateBody(LOGIN, { password: '' }))).toEqual([
      { field: 'email', key: 'validation.required' },
      { field: 'password', key: 'validation.too_short' },
    ]);
    expect(issuesOf(validateBody(LOGIN, { email: 'a'.repeat(255), password: 7 }))).toEqual([
      { field: 'email', key: 'validation.too_long' },
      { field: 'password', key: 'validation.type' },
    ]);
    expect(issuesOf(validateBody(LOGIN, { email: 'a', password: 'b', remember_me: true }))).toEqual(
      [{ field: 'remember_me', key: 'validation.unknown_field' }],
    );
  });

  it('en el borde: 254 caracteres valen y 255 no; el vacío no vale', () => {
    expect(validateBody(LOGIN, { email: 'a'.repeat(254), password: 'b' }).ok).toBe(true);
    expect(issuesOf(validateBody(LOGIN, { email: '', password: 'b' }))).toEqual([
      { field: 'email', key: 'validation.too_short' },
    ]);
  });

  it('lo que no es un objeto se rechaza sin lanzar', () => {
    for (const body of [null, undefined, 'x', 1, []]) {
      expect(issuesOf(validateBody(LOGIN, body))).toEqual([
        { field: null, key: 'validation.type' },
      ]);
    }
  });
});

describe('validateQuery', () => {
  it('rango, enumeración, fecha y hora y campo fuera del tipo', () => {
    expect(validateQuery(NOTIFICATIONS, {}).ok).toBe(true);
    expect(validateQuery(NOTIFICATIONS, { limit: 200, since: '2026-10-23T10:00:00.000Z' }).ok).toBe(
      true,
    );
    expect(
      issuesOf(
        validateQuery(NOTIFICATIONS, {
          limit: 0,
          status: 'archived',
          since: 'ayer',
          page: 2,
        }),
      ),
    ).toEqual([
      { field: 'page', key: 'validation.unknown_field' },
      { field: 'limit', key: 'validation.out_of_range' },
      { field: 'status', key: 'validation.not_allowed' },
      { field: 'since', key: 'validation.format_date_time' },
    ]);
    expect(issuesOf(validateQuery(NOTIFICATIONS, { limit: 201 }))).toEqual([
      { field: 'limit', key: 'validation.out_of_range' },
    ]);
  });

  it('identificador y patrón anclado del periodo', () => {
    expect(validateQuery(EXPOSURE, { zone_id: ZONE, period: '2026-10' }).ok).toBe(true);
    expect(issuesOf(validateQuery(EXPOSURE, { zone_id: 'zona-1', period: '2026-13' }))).toEqual([
      { field: 'zone_id', key: 'validation.format_uuid' },
      { field: 'period', key: 'validation.pattern' },
    ]);
    expect(issuesOf(validateQuery(EXPOSURE, { zone_id: ZONE, period: 'x2026-10' }))).toEqual([
      { field: 'period', key: 'validation.pattern' },
    ]);
  });

  it('toda clave que puede devolver tiene texto en el catálogo', () => {
    const keys = [
      'validation.required',
      'validation.unknown_field',
      'validation.too_short',
      'validation.too_long',
      'validation.too_few_items',
      'validation.too_many_items',
      'validation.duplicate_items',
      'validation.out_of_range',
      'validation.pattern',
      'validation.not_allowed',
      'validation.type',
      'validation.format_date_time',
      'validation.format_uuid',
      'validation.format_email',
      'validation.invalid',
    ];
    for (const key of keys) expect(texts[key], key).toBeTypeOf('string');
  });
});

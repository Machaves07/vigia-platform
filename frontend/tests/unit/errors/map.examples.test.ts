// @vitest-environment node
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import platform from '../../../../backend/resources/labels.platform.es.json';
import loop from '../../../../backend/resources/labels.loop.es.json';
import { detailLabel, exportStateLabel } from '@/ports/errors/labels';
import { behaviourFor, counts_for_circuit } from '@/ports/errors/map';
import texts from '@/texts/errors.json';

const rows = [
  ['unauthenticated', 401, 'reauth', 'errors.reauth'],
  ['second_factor_required', 401, 'second_factor', 'errors.second_factor'],
  ['privacy_notice_required', 403, 'privacy_notice', 'errors.privacy_notice'],
  ['forbidden', 403, 'forbidden_reload', 'errors.forbidden_reload'],
  ['not_found', 404, 'not_available_in_scope', 'errors.not_available_in_scope'],
  ['invalid_request', 400, 'anchored', 'errors.anchored'],
  ['conflict', 409, 'anchored', 'errors.anchored'],
  ['payload_too_large', 413, 'payload_too_large', 'errors.payload_too_large'],
  ['throttled', 429, 'countdown', 'errors.countdown'],
  ['rate_limited', 429, 'countdown', 'errors.countdown'],
  ['temporarily_unavailable', 503, 'transient', 'errors.transient'],
  ['period_too_long', 400, 'period_too_long', 'errors.period_too_long'],
  ['zone_without_node', 409, 'zone_without_node', 'errors.zone_without_node'],
  ['storage_unavailable', 503, 'degraded_section', 'errors.storage_unavailable'],
  ['internal_error', 500, 'internal', 'errors.internal_error'],
] as const;

const dependencies = [
  ['loop_gate_state_unavailable', 'gate_state'],
  ['loop_standard_text_unavailable', 'standard_text'],
  ['accreditation_coverage_unavailable', 'coverage'],
  ['loop_unclosable_policy_unavailable', 'unclosable_policy'],
  ['loop_assistant_unavailable', 'assistant'],
] as const;

const lists = {
  catalog: platform.catalog_detail_code,
  fleet: platform.fleet_detail_code,
  loop: loop.loop_detail_code,
  accreditation: loop.accreditation_detail_code,
  notifications: loop.notifications_detail_code,
};

function source(path: string): string {
  return readFileSync(new URL(`../../../../backend/${path}`, import.meta.url), 'utf8');
}

describe('BR-APP-71 y notas: una fila por código', () => {
  it.each(rows)('%s → %s / %s', (code, status, kind, text_key) => {
    const result = behaviourFor({ code, status, retry_after_seconds: 5 });
    expect(result).toMatchObject({ kind, text_key, message_es: texts[text_key] });
    expect(counts_for_circuit({ code, status })).toBe(code === 'temporarily_unavailable');
  });

  it('la tabla tiene exactamente los 15 ApiErrorCode del backend', () => {
    const python = source('src/vigia_platform/shared/api/errors.py');
    const block = python
      .split('class ApiErrorCode(enum.StrEnum):')[1]
      ?.split('\n\nHTTP_STATUS:')[0];
    expect(block).toBeDefined();
    const codes = [...(block ?? '').matchAll(/^    [A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]);
    expect(codes).toHaveLength(15);
    expect(new Set(rows.map(([code]) => code))).toEqual(new Set(codes));
  });

  it.each(['csrf_rejected', 'not_available_in_scope', '', '__proto__', 'toString'])(
    'desconocido %s es interno',
    (code) => {
      expect(behaviourFor({ code, status: 503, detail_code: 'loop_reason_required' })).toEqual({
        kind: 'internal',
        text_key: 'errors.internal_error',
        message_es: texts['errors.internal_error'],
      });
    },
  );

  it('forbidden y not_found no comparten clave ni muestran mensaje o detalle ajeno', () => {
    const input = {
      status: 403,
      detail_code: 'loop_reason_required',
      message_es: 'Mensaje que no debe salir',
    };
    expect(behaviourFor({ ...input, code: 'forbidden' }).text_key).toBe('errors.forbidden_reload');
    expect(behaviourFor({ ...input, code: 'not_found' }).text_key).toBe(
      'errors.not_available_in_scope',
    );
    expect(behaviourFor({ ...input, code: 'not_found' }).message_es).toBe(
      texts['errors.not_available_in_scope'],
    );
  });

  it('reauth conserva ruta y borrador; errores de formulario conservan lo escrito', () => {
    expect(behaviourFor({ code: 'unauthenticated', status: 401 })).toMatchObject({
      preserve_draft: true,
      preserve_route: true,
    });
    for (const code of ['invalid_request', 'conflict', 'payload_too_large']) {
      expect(behaviourFor({ code, status: 400 })).toMatchObject({ preserve_draft: true });
    }
  });
});

describe('negocio, A-56/A-68 y diferencias del hito U-04', () => {
  it.each([
    ['invalid_request', 400, 'catalog_admission_rejected'],
    ['invalid_request', 400, 'loop_reason_required'],
    ['conflict', 409, 'loop_transition_not_allowed'],
    ['conflict', 409, 'accreditation_monthly_package_not_available'],
  ])('%s %s con %s queda anclado y etiquetado', (code, status, detail_code) => {
    expect(
      behaviourFor({
        code,
        status,
        detail_code,
        message_es: 'Mensaje genérico del servidor',
      }),
    ).toMatchObject({
      kind: 'anchored',
      preserve_draft: true,
      message_es: detailLabel(detail_code),
    });
  });

  it.each(dependencies)(
    '503 %s degrada solo %s y no cuenta para el circuito',
    (detail_code, section) => {
      const error = { code: 'temporarily_unavailable', status: 503, detail_code };
      expect(behaviourFor(error)).toEqual({
        kind: 'degraded_section',
        section,
        text_key: 'errors.degraded_section',
        message_es: detailLabel(detail_code),
      });
      expect(counts_for_circuit(error)).toBe(false);
    },
  );

  it('503 sin detalle transitorio cuenta, storage sin detalle solo degrada el almacén', () => {
    expect(behaviourFor({ code: 'temporarily_unavailable', status: 503 })).toMatchObject({
      kind: 'transient',
      retry_after_seconds: null,
    });
    expect(counts_for_circuit({ code: 'temporarily_unavailable', status: 503 })).toBe(true);
    expect(behaviourFor({ code: 'storage_unavailable', status: 503 })).toMatchObject({
      kind: 'degraded_section',
      section: 'storage',
    });
    expect(counts_for_circuit({ code: 'storage_unavailable', status: 503 })).toBe(false);
    expect(counts_for_circuit({ kind: 'timeout' })).toBe(true);
    expect(counts_for_circuit({ kind: 'network' })).toBe(true);
  });

  it.each([
    'catalog_new_rejection',
    'fleet_new_rejection',
    'loop_new_rejection',
    'accreditation_new_rejection',
    'notifications_new_rejection',
    'app_future_rejection',
  ])('detalle futuro %s usa message_es', (detail_code) => {
    const error = {
      code: 'invalid_request',
      status: 400,
      detail_code,
      body: { message_es: 'Mensaje público del servidor' },
    };
    expect(behaviourFor(error)).toMatchObject({
      kind: 'anchored',
      message_es: error.body.message_es,
    });
    expect(behaviourFor({ ...error, code: 'temporarily_unavailable', status: 503 })).toMatchObject({
      kind: 'degraded_section',
      message_es: error.body.message_es,
    });
    expect(counts_for_circuit({ ...error, code: 'temporarily_unavailable' })).toBe(false);
  });

  it.each(['invalid_request', 'conflict'])(
    'sin detalle %s usa el mensaje público, no body crudo',
    (code) => {
      expect(
        behaviourFor({
          code,
          status: 400,
          body: { message_es: 'Revise el campo indicado', trace: 'Dato interno' },
        }),
      ).toEqual({
        kind: 'anchored',
        text_key: 'errors.anchored',
        message_es: 'Revise el campo indicado',
        preserve_draft: true,
      });
    },
  );

  it.each(Object.entries(loop.export_state))(
    '409 sin detail_code muestra export_state %s con etiqueta',
    (export_state, export_state_label) => {
      expect(
        behaviourFor({
          code: 'conflict',
          status: 409,
          body: { export_state, url: 'No debe salir', trace: 'No debe salir' },
        }),
      ).toEqual({
        kind: 'anchored',
        text_key: 'errors.export_state',
        message_es: texts['errors.export_state'],
        preserve_draft: true,
        export_state,
        export_state_label,
      });
    },
  );

  it.each([
    null,
    [],
    'expired',
    { export_state: 'composing' },
    { export_state: '__proto__' },
    { export_state: 1 },
  ])('cuerpo de exportación inválido %j no se expone', (body) => {
    expect(behaviourFor({ code: 'conflict', status: 409, body }).text_key).toBe('errors.anchored');
  });

  it('export_state no sustituye un detalle de negocio ni otro code/status', () => {
    const error = { code: 'conflict', status: 409, body: { export_state: 'expired' } };
    expect(behaviourFor({ ...error, detail_code: 'loop_transition_not_allowed' }).text_key).toBe(
      'errors.anchored',
    );
    expect(behaviourFor({ ...error, status: 400 }).text_key).toBe('errors.anchored');
    expect(behaviourFor({ ...error, code: 'invalid_request' }).text_key).toBe('errors.anchored');
  });

  it.each([1, 3600, 0, -1, 3601, 1.5, NaN, Infinity, null, undefined])(
    'retardo %s solo se conserva dentro del contrato',
    (retry_after_seconds) => {
      const extra = retry_after_seconds === undefined ? {} : { retry_after_seconds };
      for (const code of ['throttled', 'rate_limited', 'temporarily_unavailable']) {
        expect(behaviourFor({ code, status: 503, ...extra })).toMatchObject({
          retry_after_seconds:
            retry_after_seconds === 1 || retry_after_seconds === 3600 ? retry_after_seconds : null,
        });
      }
    },
  );

  it.each(['', ' ', 'x'.repeat(257), 7, null])(
    'mensaje público inválido %j usa texto genérico',
    (message_es) => {
      expect(
        behaviourFor({ code: 'invalid_request', status: 400, body: { message_es } }).message_es,
      ).toBe(texts['errors.anchored']);
    },
  );

  it.each(['toString', '__proto__', 'constructor'])('las búsquedas no heredan %s', (code) => {
    expect(detailLabel(code)).toBeUndefined();
    expect(exportStateLabel(code)).toBeUndefined();
  });
});

describe('bordes del contrato de errores', () => {
  it('un detalle debe tener prefijo, forma completa y como máximo 64 caracteres', () => {
    const atLimit = `loop_${'a'.repeat(59)}`;
    const input = { code: 'temporarily_unavailable', status: 503 };
    expect(counts_for_circuit({ ...input, detail_code: atLimit })).toBe(false);
    for (const detail_code of [
      `${atLimit}a`,
      'loop_',
      'other_error',
      'loop_bad\n',
      'loop_bad\r\n',
      'loop_Bad',
      'loop_bad__name',
    ]) {
      expect(behaviourFor({ ...input, detail_code }).kind).toBe('transient');
      expect(counts_for_circuit({ ...input, detail_code })).toBe(true);
    }
  });

  it('el mensaje público admite exactamente 256 caracteres', () => {
    const message_es = 'a'.repeat(256);
    expect(behaviourFor({ code: 'invalid_request', status: 400, message_es }).message_es).toBe(
      message_es,
    );
  });
});

describe('igualdad de conjuntos de las cinco listas', () => {
  it.each(Object.entries(lists))(
    '%s coincide exactamente con su enum backend y con las etiquetas consumidas',
    (module, labels) => {
      const python = source(`src/vigia_platform/${module}/detail_codes.py`);
      const values = [...python.matchAll(/^    [A-Z_]+ = "([a-z_]+)"/gm)].map((m) => m[1]);
      expect(values.length).toBeGreaterThan(0);
      expect(new Set(Object.keys(labels))).toEqual(new Set(values));
      for (const [code, label] of Object.entries(labels)) {
        expect(detailLabel(code)).toBe(label);
        expect(
          behaviourFor({ code: 'invalid_request', status: 400, detail_code: code }).message_es,
        ).toBe(label);
      }
    },
  );

  it('todo detail_code literal de app.yaml tiene etiqueta en las cinco listas', () => {
    // El exportador fija indentación y comillas. Solo enums *DetailCode y campos detail_code:
    // operationId, catalog_version y catalog_change no son errores de negocio.
    const document = source('openapi/app.yaml');
    const schemas = [
      ...document.matchAll(/^    "([A-Za-z0-9_]+)":\n([\s\S]*?)(?=^    "|$(?![\s\S]))/gm),
    ]
      .filter((match) => match[1]?.endsWith('DetailCode'))
      .map((match) => match[2]);
    const fields = [
      ...document.matchAll(/^([ ]+)"detail_code":\n([\s\S]*?)(?=^\1"|$(?![\s\S]))/gm),
    ].map((match) => match[2]);
    const codes = [
      ...[...schemas, ...fields]
        .join('\n')
        .matchAll(/"((?:catalog|fleet|loop|accreditation|notifications|app)_[a-z0-9_]+)"/g),
    ].map((m) => m[1]);
    expect(codes.length).toBeGreaterThan(0);
    const labelled = new Set(Object.values(lists).flatMap((labels) => Object.keys(labels)));
    expect([...new Set(codes)].filter((code) => !labelled.has(code ?? ''))).toEqual([]);
  });
});

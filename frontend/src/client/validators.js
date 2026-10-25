/* eslint-disable -- código autónomo de Ajv: no se edita ni se revisa a mano. */
// @ts-nocheck -- código autónomo de Ajv; sus tipos están en validators.d.ts.
// Generado por `npm run generate` (scripts/generate.mjs) desde backend/openapi/app.yaml: no se edita a mano; `npm run generate:check` exige diferencia vacía (NFR-APP-48).
import * as runtime0Module from 'ajv/dist/runtime/ucs2length.js';
import * as runtime1Module from 'ajv-formats/dist/formats.js';
const cjs = (module) => (module.default?.__esModule ? module.default : module);
const runtime0 = cjs(runtime0Module);
const runtime1 = cjs(runtime1Module);
export const body__accept_invitations__token__accept_post = validate52;
const schema19 = {
  oneOf: [
    { $ref: 'components.json#/$defs/BeginActivation' },
    { $ref: 'components.json#/$defs/CompleteActivation' },
  ],
};
const schema21 = {
  properties: { step: { type: 'string', enum: ['begin'] } },
  additionalProperties: false,
  type: 'object',
  required: ['step'],
};

function validate54(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate54.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.step === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'step' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'step')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.step !== undefined) {
      let data0 = data.step;
      if (typeof data0 !== 'string') {
        const err2 = {
          instancePath: instancePath + '/step',
          schemaPath: '#/properties/step/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      if (!(data0 === 'begin')) {
        const err3 = {
          instancePath: instancePath + '/step',
          schemaPath: '#/properties/step/enum',
          keyword: 'enum',
          params: { allowedValues: schema21.properties.step.enum },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
  } else {
    const err4 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err4];
    } else {
      vErrors.push(err4);
    }
    errors++;
  }
  validate54.errors = vErrors;
  return errors === 0;
}
validate54.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema22 = {
  properties: {
    step: { type: 'string', enum: ['complete'] },
    password: { type: 'string', maxLength: 1024, minLength: 1 },
    notice_version: { type: 'string', maxLength: 32, minLength: 1 },
    second_factor_code: {
      anyOf: [{ type: 'string', maxLength: 16, minLength: 1 }, { type: 'null' }],
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['step', 'password', 'notice_version'],
};
const func1 = runtime0.default;

function validate56(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate56.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.step === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'step' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.password === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'password' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.notice_version === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'notice_version' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'step' ||
        key0 === 'password' ||
        key0 === 'notice_version' ||
        key0 === 'second_factor_code'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.step !== undefined) {
      let data0 = data.step;
      if (typeof data0 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/step',
          schemaPath: '#/properties/step/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      if (!(data0 === 'complete')) {
        const err5 = {
          instancePath: instancePath + '/step',
          schemaPath: '#/properties/step/enum',
          keyword: 'enum',
          params: { allowedValues: schema22.properties.step.enum },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.password !== undefined) {
      let data1 = data.password;
      if (typeof data1 === 'string') {
        if (func1(data1) > 1024) {
          const err6 = {
            instancePath: instancePath + '/password',
            schemaPath: '#/properties/password/maxLength',
            keyword: 'maxLength',
            params: { limit: 1024 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err7 = {
            instancePath: instancePath + '/password',
            schemaPath: '#/properties/password/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/password',
          schemaPath: '#/properties/password/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.notice_version !== undefined) {
      let data2 = data.notice_version;
      if (typeof data2 === 'string') {
        if (func1(data2) > 32) {
          const err9 = {
            instancePath: instancePath + '/notice_version',
            schemaPath: '#/properties/notice_version/maxLength',
            keyword: 'maxLength',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
        if (func1(data2) < 1) {
          const err10 = {
            instancePath: instancePath + '/notice_version',
            schemaPath: '#/properties/notice_version/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/notice_version',
          schemaPath: '#/properties/notice_version/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
    }
    if (data.second_factor_code !== undefined) {
      let data3 = data.second_factor_code;
      const _errs9 = errors;
      let valid1 = false;
      const _errs10 = errors;
      if (typeof data3 === 'string') {
        if (func1(data3) > 16) {
          const err12 = {
            instancePath: instancePath + '/second_factor_code',
            schemaPath: '#/properties/second_factor_code/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 16 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
        if (func1(data3) < 1) {
          const err13 = {
            instancePath: instancePath + '/second_factor_code',
            schemaPath: '#/properties/second_factor_code/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/second_factor_code',
          schemaPath: '#/properties/second_factor_code/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      const _errs12 = errors;
      if (data3 !== null) {
        const err15 = {
          instancePath: instancePath + '/second_factor_code',
          schemaPath: '#/properties/second_factor_code/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid0 = _errs12 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err16 = {
          instancePath: instancePath + '/second_factor_code',
          schemaPath: '#/properties/second_factor_code/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err17 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err17];
    } else {
      vErrors.push(err17);
    }
    errors++;
  }
  validate56.errors = vErrors;
  return errors === 0;
}
validate56.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate52(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate52.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  const _errs0 = errors;
  let valid0 = false;
  let passing0 = null;
  const _errs1 = errors;
  if (
    !validate54(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate54.errors : vErrors.concat(validate54.errors);
    errors = vErrors.length;
  }
  var _valid0 = _errs1 === errors;
  if (_valid0) {
    valid0 = true;
    passing0 = 0;
    var props0 = true;
  }
  const _errs2 = errors;
  if (
    !validate56(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate56.errors : vErrors.concat(validate56.errors);
    errors = vErrors.length;
  }
  var _valid0 = _errs2 === errors;
  if (_valid0 && valid0) {
    valid0 = false;
    passing0 = [passing0, 1];
  } else {
    if (_valid0) {
      valid0 = true;
      passing0 = 1;
      if (props0 !== true) {
        props0 = true;
      }
    }
  }
  if (!valid0) {
    const err0 = {
      instancePath,
      schemaPath: '#/oneOf',
      keyword: 'oneOf',
      params: { passingSchemas: passing0 },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  } else {
    errors = _errs0;
    if (vErrors !== null) {
      if (_errs0) {
        vErrors.length = _errs0;
      } else {
        vErrors = null;
      }
    }
  }
  validate52.errors = vErrors;
  evaluated0.props = props0;
  return errors === 0;
}
validate52.evaluated = { dynamicProps: true, dynamicItems: false };

export const body__accept_notice_privacy_notice_accept_post = validate58;
const schema23 = { $ref: 'components.json#/$defs/NoticeAcceptRequest' };
const schema24 = {
  properties: { notice_version: { type: 'string', maxLength: 32, minLength: 1 } },
  additionalProperties: false,
  type: 'object',
  required: ['notice_version'],
};

function validate59(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate59.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.notice_version === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'notice_version' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'notice_version')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.notice_version !== undefined) {
      let data0 = data.notice_version;
      if (typeof data0 === 'string') {
        if (func1(data0) > 32) {
          const err2 = {
            instancePath: instancePath + '/notice_version',
            schemaPath: '#/properties/notice_version/maxLength',
            keyword: 'maxLength',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err3 = {
            instancePath: instancePath + '/notice_version',
            schemaPath: '#/properties/notice_version/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/notice_version',
          schemaPath: '#/properties/notice_version/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate59.errors = vErrors;
  return errors === 0;
}
validate59.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate58(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate58.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate59(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate59.errors : vErrors.concat(validate59.errors);
    errors = vErrors.length;
  }
  validate58.errors = vErrors;
  return errors === 0;
}
validate58.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__add_evidence_loop_actions__action_id__evidence_post = validate61;
const schema25 = { $ref: 'components.json#/$defs/EvidenceEntryBody' };
const schema26 = {
  properties: {
    description: { type: 'string', maxLength: 2000, minLength: 20 },
    performed_at: { type: 'string', maxLength: 24, minLength: 24 },
    performed_by_role: { type: 'string', maxLength: 64, minLength: 1 },
    verification_note: {
      anyOf: [{ type: 'string', maxLength: 1000, minLength: 1 }, { type: 'null' }],
    },
    attachments: {
      items: { $ref: 'components.json#/$defs/EvidenceAttachmentBody' },
      type: 'array',
      maxItems: 20,
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['description', 'performed_at', 'performed_by_role'],
};
const schema27 = {
  properties: {
    attachment_id: { type: 'string', format: 'uuid' },
    content_type: { type: 'string', maxLength: 255, minLength: 1 },
    size_bytes: { type: 'integer', minimum: 1 },
    sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
    filename: { type: 'string', maxLength: 128, minLength: 1 },
    no_identifiable_person_declared: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['attachment_id', 'content_type', 'size_bytes', 'sha256', 'filename'],
};
const formats0 = /^(?:urn:uuid:)?[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
const pattern3 = new RegExp('^[0-9a-f]{64}$', 'u');

function validate63(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate63.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.attachment_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'attachment_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.content_type === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'content_type' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.size_bytes === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'size_bytes' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.sha256 === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'sha256' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.filename === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'filename' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'attachment_id' ||
        key0 === 'content_type' ||
        key0 === 'size_bytes' ||
        key0 === 'sha256' ||
        key0 === 'filename' ||
        key0 === 'no_identifiable_person_declared'
      )) {
        const err5 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.attachment_id !== undefined) {
      let data0 = data.attachment_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err6 = {
            instancePath: instancePath + '/attachment_id',
            schemaPath: '#/properties/attachment_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/attachment_id',
          schemaPath: '#/properties/attachment_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.content_type !== undefined) {
      let data1 = data.content_type;
      if (typeof data1 === 'string') {
        if (func1(data1) > 255) {
          const err8 = {
            instancePath: instancePath + '/content_type',
            schemaPath: '#/properties/content_type/maxLength',
            keyword: 'maxLength',
            params: { limit: 255 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err9 = {
            instancePath: instancePath + '/content_type',
            schemaPath: '#/properties/content_type/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
      } else {
        const err10 = {
          instancePath: instancePath + '/content_type',
          schemaPath: '#/properties/content_type/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
    if (data.size_bytes !== undefined) {
      let data2 = data.size_bytes;
      if (!(typeof data2 == 'number' && !(data2 % 1) && !isNaN(data2) && isFinite(data2))) {
        const err11 = {
          instancePath: instancePath + '/size_bytes',
          schemaPath: '#/properties/size_bytes/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      if (typeof data2 == 'number' && isFinite(data2)) {
        if (data2 < 1 || isNaN(data2)) {
          const err12 = {
            instancePath: instancePath + '/size_bytes',
            schemaPath: '#/properties/size_bytes/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
      }
    }
    if (data.sha256 !== undefined) {
      let data3 = data.sha256;
      if (typeof data3 === 'string') {
        if (!pattern3.test(data3)) {
          const err13 = {
            instancePath: instancePath + '/sha256',
            schemaPath: '#/properties/sha256/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9a-f]{64}$' },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/sha256',
          schemaPath: '#/properties/sha256/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
    }
    if (data.filename !== undefined) {
      let data4 = data.filename;
      if (typeof data4 === 'string') {
        if (func1(data4) > 128) {
          const err15 = {
            instancePath: instancePath + '/filename',
            schemaPath: '#/properties/filename/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
        if (func1(data4) < 1) {
          const err16 = {
            instancePath: instancePath + '/filename',
            schemaPath: '#/properties/filename/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err16];
          } else {
            vErrors.push(err16);
          }
          errors++;
        }
      } else {
        const err17 = {
          instancePath: instancePath + '/filename',
          schemaPath: '#/properties/filename/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
    }
    if (data.no_identifiable_person_declared !== undefined) {
      if (typeof data.no_identifiable_person_declared !== 'boolean') {
        const err18 = {
          instancePath: instancePath + '/no_identifiable_person_declared',
          schemaPath: '#/properties/no_identifiable_person_declared/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      }
    }
  } else {
    const err19 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err19];
    } else {
      vErrors.push(err19);
    }
    errors++;
  }
  validate63.errors = vErrors;
  return errors === 0;
}
validate63.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate62(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate62.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.description === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'description' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.performed_at === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'performed_at' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.performed_by_role === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'performed_by_role' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'description' ||
        key0 === 'performed_at' ||
        key0 === 'performed_by_role' ||
        key0 === 'verification_note' ||
        key0 === 'attachments'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.description !== undefined) {
      let data0 = data.description;
      if (typeof data0 === 'string') {
        if (func1(data0) > 2000) {
          const err4 = {
            instancePath: instancePath + '/description',
            schemaPath: '#/properties/description/maxLength',
            keyword: 'maxLength',
            params: { limit: 2000 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        if (func1(data0) < 20) {
          const err5 = {
            instancePath: instancePath + '/description',
            schemaPath: '#/properties/description/minLength',
            keyword: 'minLength',
            params: { limit: 20 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/description',
          schemaPath: '#/properties/description/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.performed_at !== undefined) {
      let data1 = data.performed_at;
      if (typeof data1 === 'string') {
        if (func1(data1) > 24) {
          const err7 = {
            instancePath: instancePath + '/performed_at',
            schemaPath: '#/properties/performed_at/maxLength',
            keyword: 'maxLength',
            params: { limit: 24 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (func1(data1) < 24) {
          const err8 = {
            instancePath: instancePath + '/performed_at',
            schemaPath: '#/properties/performed_at/minLength',
            keyword: 'minLength',
            params: { limit: 24 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/performed_at',
          schemaPath: '#/properties/performed_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.performed_by_role !== undefined) {
      let data2 = data.performed_by_role;
      if (typeof data2 === 'string') {
        if (func1(data2) > 64) {
          const err10 = {
            instancePath: instancePath + '/performed_by_role',
            schemaPath: '#/properties/performed_by_role/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
        if (func1(data2) < 1) {
          const err11 = {
            instancePath: instancePath + '/performed_by_role',
            schemaPath: '#/properties/performed_by_role/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/performed_by_role',
          schemaPath: '#/properties/performed_by_role/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
    }
    if (data.verification_note !== undefined) {
      let data3 = data.verification_note;
      const _errs9 = errors;
      let valid1 = false;
      const _errs10 = errors;
      if (typeof data3 === 'string') {
        if (func1(data3) > 1000) {
          const err13 = {
            instancePath: instancePath + '/verification_note',
            schemaPath: '#/properties/verification_note/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
        if (func1(data3) < 1) {
          const err14 = {
            instancePath: instancePath + '/verification_note',
            schemaPath: '#/properties/verification_note/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err14];
          } else {
            vErrors.push(err14);
          }
          errors++;
        }
      } else {
        const err15 = {
          instancePath: instancePath + '/verification_note',
          schemaPath: '#/properties/verification_note/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      const _errs12 = errors;
      if (data3 !== null) {
        const err16 = {
          instancePath: instancePath + '/verification_note',
          schemaPath: '#/properties/verification_note/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid0 = _errs12 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err17 = {
          instancePath: instancePath + '/verification_note',
          schemaPath: '#/properties/verification_note/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.attachments !== undefined) {
      let data4 = data.attachments;
      if (Array.isArray(data4)) {
        if (data4.length > 20) {
          const err18 = {
            instancePath: instancePath + '/attachments',
            schemaPath: '#/properties/attachments/maxItems',
            keyword: 'maxItems',
            params: { limit: 20 },
          };
          if (vErrors === null) {
            vErrors = [err18];
          } else {
            vErrors.push(err18);
          }
          errors++;
        }
        const len0 = data4.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate63(data4[i0], {
              instancePath: instancePath + '/attachments/' + i0,
              parentData: data4,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate63.errors : vErrors.concat(validate63.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err19 = {
          instancePath: instancePath + '/attachments',
          schemaPath: '#/properties/attachments/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      }
    }
  } else {
    const err20 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err20];
    } else {
      vErrors.push(err20);
    }
    errors++;
  }
  validate62.errors = vErrors;
  return errors === 0;
}
validate62.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate61(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate61.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate62(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate62.errors : vErrors.concat(validate62.errors);
    errors = vErrors.length;
  }
  validate61.errors = vErrors;
  return errors === 0;
}
validate61.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__assign_loop_findings__finding_state_id__actions_post = validate66;
const schema28 = { $ref: 'components.json#/$defs/CorrectiveActionBody' };
const schema29 = {
  properties: {
    action_type: { type: 'string', maxLength: 64 },
    action_description: { type: 'string', maxLength: 2000, minLength: 20 },
    expected_outcome: {
      anyOf: [{ type: 'string', maxLength: 1000, minLength: 1 }, { type: 'null' }],
    },
    assignee_user_id: { type: 'string', format: 'uuid' },
    deadline_days: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    supersedes_action_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['action_type', 'action_description', 'assignee_user_id'],
};

function validate67(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate67.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.action_type === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'action_type' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.action_description === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'action_description' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.assignee_user_id === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'assignee_user_id' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'action_type' ||
        key0 === 'action_description' ||
        key0 === 'expected_outcome' ||
        key0 === 'assignee_user_id' ||
        key0 === 'deadline_days' ||
        key0 === 'supersedes_action_id'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.action_type !== undefined) {
      let data0 = data.action_type;
      if (typeof data0 === 'string') {
        if (func1(data0) > 64) {
          const err4 = {
            instancePath: instancePath + '/action_type',
            schemaPath: '#/properties/action_type/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/action_type',
          schemaPath: '#/properties/action_type/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.action_description !== undefined) {
      let data1 = data.action_description;
      if (typeof data1 === 'string') {
        if (func1(data1) > 2000) {
          const err6 = {
            instancePath: instancePath + '/action_description',
            schemaPath: '#/properties/action_description/maxLength',
            keyword: 'maxLength',
            params: { limit: 2000 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data1) < 20) {
          const err7 = {
            instancePath: instancePath + '/action_description',
            schemaPath: '#/properties/action_description/minLength',
            keyword: 'minLength',
            params: { limit: 20 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/action_description',
          schemaPath: '#/properties/action_description/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.expected_outcome !== undefined) {
      let data2 = data.expected_outcome;
      const _errs7 = errors;
      let valid1 = false;
      const _errs8 = errors;
      if (typeof data2 === 'string') {
        if (func1(data2) > 1000) {
          const err9 = {
            instancePath: instancePath + '/expected_outcome',
            schemaPath: '#/properties/expected_outcome/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
        if (func1(data2) < 1) {
          const err10 = {
            instancePath: instancePath + '/expected_outcome',
            schemaPath: '#/properties/expected_outcome/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      const _errs10 = errors;
      if (data2 !== null) {
        const err12 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err13 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      } else {
        errors = _errs7;
        if (vErrors !== null) {
          if (_errs7) {
            vErrors.length = _errs7;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.assignee_user_id !== undefined) {
      let data3 = data.assignee_user_id;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err14 = {
            instancePath: instancePath + '/assignee_user_id',
            schemaPath: '#/properties/assignee_user_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err14];
          } else {
            vErrors.push(err14);
          }
          errors++;
        }
      } else {
        const err15 = {
          instancePath: instancePath + '/assignee_user_id',
          schemaPath: '#/properties/assignee_user_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
    }
    if (data.deadline_days !== undefined) {
      let data4 = data.deadline_days;
      const _errs15 = errors;
      let valid2 = false;
      const _errs16 = errors;
      if (!(typeof data4 == 'number' && !(data4 % 1) && !isNaN(data4) && isFinite(data4))) {
        const err16 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid1 = _errs16 === errors;
      valid2 = valid2 || _valid1;
      const _errs18 = errors;
      if (data4 !== null) {
        const err17 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      var _valid1 = _errs18 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err18 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.supersedes_action_id !== undefined) {
      let data5 = data.supersedes_action_id;
      const _errs21 = errors;
      let valid3 = false;
      const _errs22 = errors;
      if (typeof data5 === 'string') {
        if (!formats0.test(data5)) {
          const err19 = {
            instancePath: instancePath + '/supersedes_action_id',
            schemaPath: '#/properties/supersedes_action_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err19];
          } else {
            vErrors.push(err19);
          }
          errors++;
        }
      } else {
        const err20 = {
          instancePath: instancePath + '/supersedes_action_id',
          schemaPath: '#/properties/supersedes_action_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid2 = _errs22 === errors;
      valid3 = valid3 || _valid2;
      const _errs24 = errors;
      if (data5 !== null) {
        const err21 = {
          instancePath: instancePath + '/supersedes_action_id',
          schemaPath: '#/properties/supersedes_action_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      }
      var _valid2 = _errs24 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err22 = {
          instancePath: instancePath + '/supersedes_action_id',
          schemaPath: '#/properties/supersedes_action_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err22];
        } else {
          vErrors.push(err22);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err23 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err23];
    } else {
      vErrors.push(err23);
    }
    errors++;
  }
  validate67.errors = vErrors;
  return errors === 0;
}
validate67.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate66(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate66.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate67(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate67.errors : vErrors.concat(validate67.errors);
    errors = vErrors.length;
  }
  validate66.errors = vErrors;
  return errors === 0;
}
validate66.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__assign_role_users__user_id__roles_post = validate69;
const schema30 = { $ref: 'components.json#/$defs/AssignmentBody' };
const schema31 = {
  properties: {
    role: { $ref: 'components.json#/$defs/Role' },
    scope_level: { $ref: 'components.json#/$defs/ScopeLevel' },
    scope_id: { type: 'string', format: 'uuid' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['role', 'scope_level', 'scope_id'],
};
const schema32 = {
  type: 'string',
  enum: [
    'coordinator_sst',
    'line_manager',
    'plant_manager',
    'administrator',
    'provider_installer',
    'copasst',
    'platform_operator',
  ],
};

function validate71(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate71.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'coordinator_sst' ||
    data === 'line_manager' ||
    data === 'plant_manager' ||
    data === 'administrator' ||
    data === 'provider_installer' ||
    data === 'copasst' ||
    data === 'platform_operator'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema32.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate71.errors = vErrors;
  return errors === 0;
}
validate71.evaluated = { dynamicProps: false, dynamicItems: false };

const schema33 = { type: 'string', enum: ['organization', 'plant', 'zone'] };

function validate73(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate73.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'organization' || data === 'plant' || data === 'zone')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema33.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate73.errors = vErrors;
  return errors === 0;
}
validate73.evaluated = { dynamicProps: false, dynamicItems: false };

function validate70(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate70.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.role === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'role' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.scope_level === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_level' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.scope_id === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_id' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'role' || key0 === 'scope_level' || key0 === 'scope_id')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.role !== undefined) {
      if (
        !validate71(data.role, {
          instancePath: instancePath + '/role',
          parentData: data,
          parentDataProperty: 'role',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate71.errors : vErrors.concat(validate71.errors);
        errors = vErrors.length;
      }
    }
    if (data.scope_level !== undefined) {
      if (
        !validate73(data.scope_level, {
          instancePath: instancePath + '/scope_level',
          parentData: data,
          parentDataProperty: 'scope_level',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate73.errors : vErrors.concat(validate73.errors);
        errors = vErrors.length;
      }
    }
    if (data.scope_id !== undefined) {
      let data2 = data.scope_id;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err4 = {
            instancePath: instancePath + '/scope_id',
            schemaPath: '#/properties/scope_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/scope_id',
          schemaPath: '#/properties/scope_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate70.errors = vErrors;
  return errors === 0;
}
validate70.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate69(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate69.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate70(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate70.errors : vErrors.concat(validate70.errors);
    errors = vErrors.length;
  }
  validate69.errors = vErrors;
  return errors === 0;
}
validate69.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__assign_zone_nodes__node_id__zones_post = validate76;
const schema34 = { $ref: 'components.json#/$defs/NodeZoneBody' };
const schema35 = {
  properties: { zone_id: { type: 'string', format: 'uuid' } },
  additionalProperties: false,
  type: 'object',
  required: ['zone_id'],
};

function validate77(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate77.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.zone_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'zone_id')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data0 = data.zone_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err2 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
  } else {
    const err4 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err4];
    } else {
      vErrors.push(err4);
    }
    errors++;
  }
  validate77.errors = vErrors;
  return errors === 0;
}
validate77.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate76(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate76.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate77(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate77.errors : vErrors.concat(validate77.errors);
    errors = vErrors.length;
  }
  validate76.errors = vErrors;
  return errors === 0;
}
validate76.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__cameras_zones__zone_id__cameras_put = validate79;
const schema36 = { $ref: 'components.json#/$defs/CamerasBody' };
const schema37 = {
  properties: {
    cameras: {
      items: { $ref: 'components.json#/$defs/CameraBody' },
      type: 'array',
      maxItems: 8,
      minItems: 1,
    },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['cameras', 'reason_es'],
};
const schema38 = {
  properties: {
    camera_id: { type: 'string', format: 'uuid' },
    code: { type: 'string' },
    role_in_zone: { $ref: 'components.json#/$defs/CameraRoleInZone' },
    declared_min_fps: { type: 'number' },
    stream_reference: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['camera_id', 'code', 'role_in_zone', 'declared_min_fps', 'stream_reference'],
};
const schema39 = { type: 'string', enum: ['primary', 'redundant'] };

function validate82(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate82.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'primary' || data === 'redundant')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema39.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate82.errors = vErrors;
  return errors === 0;
}
validate82.evaluated = { dynamicProps: false, dynamicItems: false };

function validate81(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate81.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.camera_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'camera_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.code === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'code' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.role_in_zone === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'role_in_zone' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.declared_min_fps === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'declared_min_fps' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.stream_reference === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'stream_reference' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'camera_id' ||
        key0 === 'code' ||
        key0 === 'role_in_zone' ||
        key0 === 'declared_min_fps' ||
        key0 === 'stream_reference'
      )) {
        const err5 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.camera_id !== undefined) {
      let data0 = data.camera_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err6 = {
            instancePath: instancePath + '/camera_id',
            schemaPath: '#/properties/camera_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/camera_id',
          schemaPath: '#/properties/camera_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.code !== undefined) {
      if (typeof data.code !== 'string') {
        const err8 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.role_in_zone !== undefined) {
      if (
        !validate82(data.role_in_zone, {
          instancePath: instancePath + '/role_in_zone',
          parentData: data,
          parentDataProperty: 'role_in_zone',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate82.errors : vErrors.concat(validate82.errors);
        errors = vErrors.length;
      }
    }
    if (data.declared_min_fps !== undefined) {
      let data3 = data.declared_min_fps;
      if (!(typeof data3 == 'number' && isFinite(data3))) {
        const err9 = {
          instancePath: instancePath + '/declared_min_fps',
          schemaPath: '#/properties/declared_min_fps/type',
          keyword: 'type',
          params: { type: 'number' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.stream_reference !== undefined) {
      if (typeof data.stream_reference !== 'string') {
        const err10 = {
          instancePath: instancePath + '/stream_reference',
          schemaPath: '#/properties/stream_reference/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
  } else {
    const err11 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err11];
    } else {
      vErrors.push(err11);
    }
    errors++;
  }
  validate81.errors = vErrors;
  return errors === 0;
}
validate81.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate80(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate80.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.cameras === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'cameras' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'cameras' || key0 === 'reason_es')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.cameras !== undefined) {
      let data0 = data.cameras;
      if (Array.isArray(data0)) {
        if (data0.length > 8) {
          const err3 = {
            instancePath: instancePath + '/cameras',
            schemaPath: '#/properties/cameras/maxItems',
            keyword: 'maxItems',
            params: { limit: 8 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (data0.length < 1) {
          const err4 = {
            instancePath: instancePath + '/cameras',
            schemaPath: '#/properties/cameras/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate81(data0[i0], {
              instancePath: instancePath + '/cameras/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate81.errors : vErrors.concat(validate81.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/cameras',
          schemaPath: '#/properties/cameras/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err6 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate80.errors = vErrors;
  return errors === 0;
}
validate80.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate79(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate79.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate80(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate80.errors : vErrors.concat(validate80.errors);
    errors = vErrors.length;
  }
  validate79.errors = vErrors;
  return errors === 0;
}
validate79.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__catalog_history_zones__zone_id__catalog_versions_get = validate86;
const schema40 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 10 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate86(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate86.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'after' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 10) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 10 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate86.errors = vErrors;
  return errors === 0;
}
validate86.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__change_configuration_loop_plants__plant_id__configuration_put = validate87;
const schema41 = { $ref: 'components.json#/$defs/ConfigurationChangeBody' };
const schema42 = {
  properties: {
    classification_deadline_business_days: {
      anyOf: [{ type: 'integer', maximum: 30, minimum: 1 }, { type: 'null' }],
    },
    review_deadline_business_days: {
      anyOf: [{ type: 'integer', maximum: 30, minimum: 1 }, { type: 'null' }],
    },
    action_deadline_default_days: {
      anyOf: [{ type: 'integer', maximum: 180, minimum: 1 }, { type: 'null' }],
    },
    action_deadline_min_days: {
      anyOf: [{ type: 'integer', maximum: 180, minimum: 1 }, { type: 'null' }],
    },
    action_deadline_max_days: {
      anyOf: [{ type: 'integer', maximum: 180, minimum: 1 }, { type: 'null' }],
    },
    manager_highlight_overdue_days: {
      anyOf: [{ type: 'integer', maximum: 365, minimum: 1 }, { type: 'null' }],
    },
    unclosable_review_default_months: {
      anyOf: [{ type: 'integer', maximum: 12, minimum: 1 }, { type: 'null' }],
    },
    unclosable_review_max_months: {
      anyOf: [{ type: 'integer', maximum: 12, minimum: 1 }, { type: 'null' }],
    },
    monthly_package_generation_day: {
      anyOf: [{ type: 'integer', maximum: 5, minimum: 1 }, { type: 'null' }],
    },
    sample_percent: { anyOf: [{ type: 'integer', maximum: 100, minimum: 1 }, { type: 'null' }] },
    sample_min: { anyOf: [{ type: 'integer', maximum: 1000, minimum: 1 }, { type: 'null' }] },
    sample_max: { anyOf: [{ type: 'integer', maximum: 1000, minimum: 1 }, { type: 'null' }] },
    adoption_silence_days: {
      anyOf: [{ type: 'integer', maximum: 90, minimum: 1 }, { type: 'null' }],
    },
    exposure_coverage_threshold_percent: {
      anyOf: [{ type: 'integer', maximum: 100, minimum: 0 }, { type: 'null' }],
    },
    email_grouping_hours: {
      anyOf: [{ type: 'integer', maximum: 24, minimum: 1 }, { type: 'null' }],
    },
    export_max_days: { anyOf: [{ type: 'integer', maximum: 3660, minimum: 1 }, { type: 'null' }] },
    export_max_zones: { anyOf: [{ type: 'integer', maximum: 500, minimum: 1 }, { type: 'null' }] },
    single_occupancy_default_window_minutes: {
      anyOf: [{ type: 'integer', maximum: 480, minimum: 15 }, { type: 'null' }],
    },
    reminder_interval_hours: {
      anyOf: [{ type: 'integer', maximum: 168, minimum: 1 }, { type: 'null' }],
    },
    business_calendar: {
      anyOf: [{ $ref: 'components.json#/$defs/BusinessCalendarBody' }, { type: 'null' }],
    },
    reason: { anyOf: [{ type: 'string', maxLength: 500, minLength: 10 }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};
const func27 = Object.prototype.hasOwnProperty;
const schema43 = {
  properties: {
    working_weekdays: { items: { type: 'integer' }, type: 'array', maxItems: 7, minItems: 1 },
    non_working_dates: { items: { type: 'string' }, type: 'array', maxItems: 400 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['working_weekdays', 'non_working_dates'],
};

function validate89(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate89.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.working_weekdays === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'working_weekdays' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.non_working_dates === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'non_working_dates' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'working_weekdays' || key0 === 'non_working_dates')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.working_weekdays !== undefined) {
      let data0 = data.working_weekdays;
      if (Array.isArray(data0)) {
        if (data0.length > 7) {
          const err3 = {
            instancePath: instancePath + '/working_weekdays',
            schemaPath: '#/properties/working_weekdays/maxItems',
            keyword: 'maxItems',
            params: { limit: 7 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (data0.length < 1) {
          const err4 = {
            instancePath: instancePath + '/working_weekdays',
            schemaPath: '#/properties/working_weekdays/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data1 = data0[i0];
          if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
            const err5 = {
              instancePath: instancePath + '/working_weekdays/' + i0,
              schemaPath: '#/properties/working_weekdays/items/type',
              keyword: 'type',
              params: { type: 'integer' },
            };
            if (vErrors === null) {
              vErrors = [err5];
            } else {
              vErrors.push(err5);
            }
            errors++;
          }
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/working_weekdays',
          schemaPath: '#/properties/working_weekdays/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.non_working_dates !== undefined) {
      let data2 = data.non_working_dates;
      if (Array.isArray(data2)) {
        if (data2.length > 400) {
          const err7 = {
            instancePath: instancePath + '/non_working_dates',
            schemaPath: '#/properties/non_working_dates/maxItems',
            keyword: 'maxItems',
            params: { limit: 400 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        const len1 = data2.length;
        for (let i1 = 0; i1 < len1; i1++) {
          if (typeof data2[i1] !== 'string') {
            const err8 = {
              instancePath: instancePath + '/non_working_dates/' + i1,
              schemaPath: '#/properties/non_working_dates/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err8];
            } else {
              vErrors.push(err8);
            }
            errors++;
          }
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/non_working_dates',
          schemaPath: '#/properties/non_working_dates/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
  } else {
    const err10 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err10];
    } else {
      vErrors.push(err10);
    }
    errors++;
  }
  validate89.errors = vErrors;
  return errors === 0;
}
validate89.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate88(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate88.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!func27.call(schema42.properties, key0)) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.classification_deadline_business_days !== undefined) {
      let data0 = data.classification_deadline_business_days;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err1 = {
          instancePath: instancePath + '/classification_deadline_business_days',
          schemaPath: '#/properties/classification_deadline_business_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      if (typeof data0 == 'number' && isFinite(data0)) {
        if (data0 > 30 || isNaN(data0)) {
          const err2 = {
            instancePath: instancePath + '/classification_deadline_business_days',
            schemaPath: '#/properties/classification_deadline_business_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 30 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        if (data0 < 1 || isNaN(data0)) {
          const err3 = {
            instancePath: instancePath + '/classification_deadline_business_days',
            schemaPath: '#/properties/classification_deadline_business_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err4 = {
          instancePath: instancePath + '/classification_deadline_business_days',
          schemaPath: '#/properties/classification_deadline_business_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/classification_deadline_business_days',
          schemaPath: '#/properties/classification_deadline_business_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.review_deadline_business_days !== undefined) {
      let data1 = data.review_deadline_business_days;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err6 = {
          instancePath: instancePath + '/review_deadline_business_days',
          schemaPath: '#/properties/review_deadline_business_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 30 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/review_deadline_business_days',
            schemaPath: '#/properties/review_deadline_business_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 30 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err8 = {
            instancePath: instancePath + '/review_deadline_business_days',
            schemaPath: '#/properties/review_deadline_business_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err9 = {
          instancePath: instancePath + '/review_deadline_business_days',
          schemaPath: '#/properties/review_deadline_business_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err10 = {
          instancePath: instancePath + '/review_deadline_business_days',
          schemaPath: '#/properties/review_deadline_business_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.action_deadline_default_days !== undefined) {
      let data2 = data.action_deadline_default_days;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (!(typeof data2 == 'number' && !(data2 % 1) && !isNaN(data2) && isFinite(data2))) {
        const err11 = {
          instancePath: instancePath + '/action_deadline_default_days',
          schemaPath: '#/properties/action_deadline_default_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      if (typeof data2 == 'number' && isFinite(data2)) {
        if (data2 > 180 || isNaN(data2)) {
          const err12 = {
            instancePath: instancePath + '/action_deadline_default_days',
            schemaPath: '#/properties/action_deadline_default_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 180 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
        if (data2 < 1 || isNaN(data2)) {
          const err13 = {
            instancePath: instancePath + '/action_deadline_default_days',
            schemaPath: '#/properties/action_deadline_default_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err14 = {
          instancePath: instancePath + '/action_deadline_default_days',
          schemaPath: '#/properties/action_deadline_default_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err15 = {
          instancePath: instancePath + '/action_deadline_default_days',
          schemaPath: '#/properties/action_deadline_default_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.action_deadline_min_days !== undefined) {
      let data3 = data.action_deadline_min_days;
      const _errs21 = errors;
      let valid4 = false;
      const _errs22 = errors;
      if (!(typeof data3 == 'number' && !(data3 % 1) && !isNaN(data3) && isFinite(data3))) {
        const err16 = {
          instancePath: instancePath + '/action_deadline_min_days',
          schemaPath: '#/properties/action_deadline_min_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      if (typeof data3 == 'number' && isFinite(data3)) {
        if (data3 > 180 || isNaN(data3)) {
          const err17 = {
            instancePath: instancePath + '/action_deadline_min_days',
            schemaPath: '#/properties/action_deadline_min_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 180 },
          };
          if (vErrors === null) {
            vErrors = [err17];
          } else {
            vErrors.push(err17);
          }
          errors++;
        }
        if (data3 < 1 || isNaN(data3)) {
          const err18 = {
            instancePath: instancePath + '/action_deadline_min_days',
            schemaPath: '#/properties/action_deadline_min_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err18];
          } else {
            vErrors.push(err18);
          }
          errors++;
        }
      }
      var _valid3 = _errs22 === errors;
      valid4 = valid4 || _valid3;
      const _errs24 = errors;
      if (data3 !== null) {
        const err19 = {
          instancePath: instancePath + '/action_deadline_min_days',
          schemaPath: '#/properties/action_deadline_min_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err20 = {
          instancePath: instancePath + '/action_deadline_min_days',
          schemaPath: '#/properties/action_deadline_min_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.action_deadline_max_days !== undefined) {
      let data4 = data.action_deadline_max_days;
      const _errs27 = errors;
      let valid5 = false;
      const _errs28 = errors;
      if (!(typeof data4 == 'number' && !(data4 % 1) && !isNaN(data4) && isFinite(data4))) {
        const err21 = {
          instancePath: instancePath + '/action_deadline_max_days',
          schemaPath: '#/properties/action_deadline_max_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      }
      if (typeof data4 == 'number' && isFinite(data4)) {
        if (data4 > 180 || isNaN(data4)) {
          const err22 = {
            instancePath: instancePath + '/action_deadline_max_days',
            schemaPath: '#/properties/action_deadline_max_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 180 },
          };
          if (vErrors === null) {
            vErrors = [err22];
          } else {
            vErrors.push(err22);
          }
          errors++;
        }
        if (data4 < 1 || isNaN(data4)) {
          const err23 = {
            instancePath: instancePath + '/action_deadline_max_days',
            schemaPath: '#/properties/action_deadline_max_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err23];
          } else {
            vErrors.push(err23);
          }
          errors++;
        }
      }
      var _valid4 = _errs28 === errors;
      valid5 = valid5 || _valid4;
      const _errs30 = errors;
      if (data4 !== null) {
        const err24 = {
          instancePath: instancePath + '/action_deadline_max_days',
          schemaPath: '#/properties/action_deadline_max_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err24];
        } else {
          vErrors.push(err24);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid5 = valid5 || _valid4;
      if (!valid5) {
        const err25 = {
          instancePath: instancePath + '/action_deadline_max_days',
          schemaPath: '#/properties/action_deadline_max_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err25];
        } else {
          vErrors.push(err25);
        }
        errors++;
      } else {
        errors = _errs27;
        if (vErrors !== null) {
          if (_errs27) {
            vErrors.length = _errs27;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.manager_highlight_overdue_days !== undefined) {
      let data5 = data.manager_highlight_overdue_days;
      const _errs33 = errors;
      let valid6 = false;
      const _errs34 = errors;
      if (!(typeof data5 == 'number' && !(data5 % 1) && !isNaN(data5) && isFinite(data5))) {
        const err26 = {
          instancePath: instancePath + '/manager_highlight_overdue_days',
          schemaPath: '#/properties/manager_highlight_overdue_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err26];
        } else {
          vErrors.push(err26);
        }
        errors++;
      }
      if (typeof data5 == 'number' && isFinite(data5)) {
        if (data5 > 365 || isNaN(data5)) {
          const err27 = {
            instancePath: instancePath + '/manager_highlight_overdue_days',
            schemaPath: '#/properties/manager_highlight_overdue_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 365 },
          };
          if (vErrors === null) {
            vErrors = [err27];
          } else {
            vErrors.push(err27);
          }
          errors++;
        }
        if (data5 < 1 || isNaN(data5)) {
          const err28 = {
            instancePath: instancePath + '/manager_highlight_overdue_days',
            schemaPath: '#/properties/manager_highlight_overdue_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err28];
          } else {
            vErrors.push(err28);
          }
          errors++;
        }
      }
      var _valid5 = _errs34 === errors;
      valid6 = valid6 || _valid5;
      const _errs36 = errors;
      if (data5 !== null) {
        const err29 = {
          instancePath: instancePath + '/manager_highlight_overdue_days',
          schemaPath: '#/properties/manager_highlight_overdue_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err29];
        } else {
          vErrors.push(err29);
        }
        errors++;
      }
      var _valid5 = _errs36 === errors;
      valid6 = valid6 || _valid5;
      if (!valid6) {
        const err30 = {
          instancePath: instancePath + '/manager_highlight_overdue_days',
          schemaPath: '#/properties/manager_highlight_overdue_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err30];
        } else {
          vErrors.push(err30);
        }
        errors++;
      } else {
        errors = _errs33;
        if (vErrors !== null) {
          if (_errs33) {
            vErrors.length = _errs33;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.unclosable_review_default_months !== undefined) {
      let data6 = data.unclosable_review_default_months;
      const _errs39 = errors;
      let valid7 = false;
      const _errs40 = errors;
      if (!(typeof data6 == 'number' && !(data6 % 1) && !isNaN(data6) && isFinite(data6))) {
        const err31 = {
          instancePath: instancePath + '/unclosable_review_default_months',
          schemaPath: '#/properties/unclosable_review_default_months/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err31];
        } else {
          vErrors.push(err31);
        }
        errors++;
      }
      if (typeof data6 == 'number' && isFinite(data6)) {
        if (data6 > 12 || isNaN(data6)) {
          const err32 = {
            instancePath: instancePath + '/unclosable_review_default_months',
            schemaPath: '#/properties/unclosable_review_default_months/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 12 },
          };
          if (vErrors === null) {
            vErrors = [err32];
          } else {
            vErrors.push(err32);
          }
          errors++;
        }
        if (data6 < 1 || isNaN(data6)) {
          const err33 = {
            instancePath: instancePath + '/unclosable_review_default_months',
            schemaPath: '#/properties/unclosable_review_default_months/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err33];
          } else {
            vErrors.push(err33);
          }
          errors++;
        }
      }
      var _valid6 = _errs40 === errors;
      valid7 = valid7 || _valid6;
      const _errs42 = errors;
      if (data6 !== null) {
        const err34 = {
          instancePath: instancePath + '/unclosable_review_default_months',
          schemaPath: '#/properties/unclosable_review_default_months/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err34];
        } else {
          vErrors.push(err34);
        }
        errors++;
      }
      var _valid6 = _errs42 === errors;
      valid7 = valid7 || _valid6;
      if (!valid7) {
        const err35 = {
          instancePath: instancePath + '/unclosable_review_default_months',
          schemaPath: '#/properties/unclosable_review_default_months/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err35];
        } else {
          vErrors.push(err35);
        }
        errors++;
      } else {
        errors = _errs39;
        if (vErrors !== null) {
          if (_errs39) {
            vErrors.length = _errs39;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.unclosable_review_max_months !== undefined) {
      let data7 = data.unclosable_review_max_months;
      const _errs45 = errors;
      let valid8 = false;
      const _errs46 = errors;
      if (!(typeof data7 == 'number' && !(data7 % 1) && !isNaN(data7) && isFinite(data7))) {
        const err36 = {
          instancePath: instancePath + '/unclosable_review_max_months',
          schemaPath: '#/properties/unclosable_review_max_months/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err36];
        } else {
          vErrors.push(err36);
        }
        errors++;
      }
      if (typeof data7 == 'number' && isFinite(data7)) {
        if (data7 > 12 || isNaN(data7)) {
          const err37 = {
            instancePath: instancePath + '/unclosable_review_max_months',
            schemaPath: '#/properties/unclosable_review_max_months/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 12 },
          };
          if (vErrors === null) {
            vErrors = [err37];
          } else {
            vErrors.push(err37);
          }
          errors++;
        }
        if (data7 < 1 || isNaN(data7)) {
          const err38 = {
            instancePath: instancePath + '/unclosable_review_max_months',
            schemaPath: '#/properties/unclosable_review_max_months/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err38];
          } else {
            vErrors.push(err38);
          }
          errors++;
        }
      }
      var _valid7 = _errs46 === errors;
      valid8 = valid8 || _valid7;
      const _errs48 = errors;
      if (data7 !== null) {
        const err39 = {
          instancePath: instancePath + '/unclosable_review_max_months',
          schemaPath: '#/properties/unclosable_review_max_months/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err39];
        } else {
          vErrors.push(err39);
        }
        errors++;
      }
      var _valid7 = _errs48 === errors;
      valid8 = valid8 || _valid7;
      if (!valid8) {
        const err40 = {
          instancePath: instancePath + '/unclosable_review_max_months',
          schemaPath: '#/properties/unclosable_review_max_months/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err40];
        } else {
          vErrors.push(err40);
        }
        errors++;
      } else {
        errors = _errs45;
        if (vErrors !== null) {
          if (_errs45) {
            vErrors.length = _errs45;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.monthly_package_generation_day !== undefined) {
      let data8 = data.monthly_package_generation_day;
      const _errs51 = errors;
      let valid9 = false;
      const _errs52 = errors;
      if (!(typeof data8 == 'number' && !(data8 % 1) && !isNaN(data8) && isFinite(data8))) {
        const err41 = {
          instancePath: instancePath + '/monthly_package_generation_day',
          schemaPath: '#/properties/monthly_package_generation_day/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err41];
        } else {
          vErrors.push(err41);
        }
        errors++;
      }
      if (typeof data8 == 'number' && isFinite(data8)) {
        if (data8 > 5 || isNaN(data8)) {
          const err42 = {
            instancePath: instancePath + '/monthly_package_generation_day',
            schemaPath: '#/properties/monthly_package_generation_day/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 5 },
          };
          if (vErrors === null) {
            vErrors = [err42];
          } else {
            vErrors.push(err42);
          }
          errors++;
        }
        if (data8 < 1 || isNaN(data8)) {
          const err43 = {
            instancePath: instancePath + '/monthly_package_generation_day',
            schemaPath: '#/properties/monthly_package_generation_day/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err43];
          } else {
            vErrors.push(err43);
          }
          errors++;
        }
      }
      var _valid8 = _errs52 === errors;
      valid9 = valid9 || _valid8;
      const _errs54 = errors;
      if (data8 !== null) {
        const err44 = {
          instancePath: instancePath + '/monthly_package_generation_day',
          schemaPath: '#/properties/monthly_package_generation_day/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err44];
        } else {
          vErrors.push(err44);
        }
        errors++;
      }
      var _valid8 = _errs54 === errors;
      valid9 = valid9 || _valid8;
      if (!valid9) {
        const err45 = {
          instancePath: instancePath + '/monthly_package_generation_day',
          schemaPath: '#/properties/monthly_package_generation_day/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err45];
        } else {
          vErrors.push(err45);
        }
        errors++;
      } else {
        errors = _errs51;
        if (vErrors !== null) {
          if (_errs51) {
            vErrors.length = _errs51;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.sample_percent !== undefined) {
      let data9 = data.sample_percent;
      const _errs57 = errors;
      let valid10 = false;
      const _errs58 = errors;
      if (!(typeof data9 == 'number' && !(data9 % 1) && !isNaN(data9) && isFinite(data9))) {
        const err46 = {
          instancePath: instancePath + '/sample_percent',
          schemaPath: '#/properties/sample_percent/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err46];
        } else {
          vErrors.push(err46);
        }
        errors++;
      }
      if (typeof data9 == 'number' && isFinite(data9)) {
        if (data9 > 100 || isNaN(data9)) {
          const err47 = {
            instancePath: instancePath + '/sample_percent',
            schemaPath: '#/properties/sample_percent/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 100 },
          };
          if (vErrors === null) {
            vErrors = [err47];
          } else {
            vErrors.push(err47);
          }
          errors++;
        }
        if (data9 < 1 || isNaN(data9)) {
          const err48 = {
            instancePath: instancePath + '/sample_percent',
            schemaPath: '#/properties/sample_percent/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err48];
          } else {
            vErrors.push(err48);
          }
          errors++;
        }
      }
      var _valid9 = _errs58 === errors;
      valid10 = valid10 || _valid9;
      const _errs60 = errors;
      if (data9 !== null) {
        const err49 = {
          instancePath: instancePath + '/sample_percent',
          schemaPath: '#/properties/sample_percent/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err49];
        } else {
          vErrors.push(err49);
        }
        errors++;
      }
      var _valid9 = _errs60 === errors;
      valid10 = valid10 || _valid9;
      if (!valid10) {
        const err50 = {
          instancePath: instancePath + '/sample_percent',
          schemaPath: '#/properties/sample_percent/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err50];
        } else {
          vErrors.push(err50);
        }
        errors++;
      } else {
        errors = _errs57;
        if (vErrors !== null) {
          if (_errs57) {
            vErrors.length = _errs57;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.sample_min !== undefined) {
      let data10 = data.sample_min;
      const _errs63 = errors;
      let valid11 = false;
      const _errs64 = errors;
      if (!(typeof data10 == 'number' && !(data10 % 1) && !isNaN(data10) && isFinite(data10))) {
        const err51 = {
          instancePath: instancePath + '/sample_min',
          schemaPath: '#/properties/sample_min/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err51];
        } else {
          vErrors.push(err51);
        }
        errors++;
      }
      if (typeof data10 == 'number' && isFinite(data10)) {
        if (data10 > 1000 || isNaN(data10)) {
          const err52 = {
            instancePath: instancePath + '/sample_min',
            schemaPath: '#/properties/sample_min/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err52];
          } else {
            vErrors.push(err52);
          }
          errors++;
        }
        if (data10 < 1 || isNaN(data10)) {
          const err53 = {
            instancePath: instancePath + '/sample_min',
            schemaPath: '#/properties/sample_min/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err53];
          } else {
            vErrors.push(err53);
          }
          errors++;
        }
      }
      var _valid10 = _errs64 === errors;
      valid11 = valid11 || _valid10;
      const _errs66 = errors;
      if (data10 !== null) {
        const err54 = {
          instancePath: instancePath + '/sample_min',
          schemaPath: '#/properties/sample_min/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err54];
        } else {
          vErrors.push(err54);
        }
        errors++;
      }
      var _valid10 = _errs66 === errors;
      valid11 = valid11 || _valid10;
      if (!valid11) {
        const err55 = {
          instancePath: instancePath + '/sample_min',
          schemaPath: '#/properties/sample_min/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err55];
        } else {
          vErrors.push(err55);
        }
        errors++;
      } else {
        errors = _errs63;
        if (vErrors !== null) {
          if (_errs63) {
            vErrors.length = _errs63;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.sample_max !== undefined) {
      let data11 = data.sample_max;
      const _errs69 = errors;
      let valid12 = false;
      const _errs70 = errors;
      if (!(typeof data11 == 'number' && !(data11 % 1) && !isNaN(data11) && isFinite(data11))) {
        const err56 = {
          instancePath: instancePath + '/sample_max',
          schemaPath: '#/properties/sample_max/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err56];
        } else {
          vErrors.push(err56);
        }
        errors++;
      }
      if (typeof data11 == 'number' && isFinite(data11)) {
        if (data11 > 1000 || isNaN(data11)) {
          const err57 = {
            instancePath: instancePath + '/sample_max',
            schemaPath: '#/properties/sample_max/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err57];
          } else {
            vErrors.push(err57);
          }
          errors++;
        }
        if (data11 < 1 || isNaN(data11)) {
          const err58 = {
            instancePath: instancePath + '/sample_max',
            schemaPath: '#/properties/sample_max/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err58];
          } else {
            vErrors.push(err58);
          }
          errors++;
        }
      }
      var _valid11 = _errs70 === errors;
      valid12 = valid12 || _valid11;
      const _errs72 = errors;
      if (data11 !== null) {
        const err59 = {
          instancePath: instancePath + '/sample_max',
          schemaPath: '#/properties/sample_max/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err59];
        } else {
          vErrors.push(err59);
        }
        errors++;
      }
      var _valid11 = _errs72 === errors;
      valid12 = valid12 || _valid11;
      if (!valid12) {
        const err60 = {
          instancePath: instancePath + '/sample_max',
          schemaPath: '#/properties/sample_max/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err60];
        } else {
          vErrors.push(err60);
        }
        errors++;
      } else {
        errors = _errs69;
        if (vErrors !== null) {
          if (_errs69) {
            vErrors.length = _errs69;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.adoption_silence_days !== undefined) {
      let data12 = data.adoption_silence_days;
      const _errs75 = errors;
      let valid13 = false;
      const _errs76 = errors;
      if (!(typeof data12 == 'number' && !(data12 % 1) && !isNaN(data12) && isFinite(data12))) {
        const err61 = {
          instancePath: instancePath + '/adoption_silence_days',
          schemaPath: '#/properties/adoption_silence_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err61];
        } else {
          vErrors.push(err61);
        }
        errors++;
      }
      if (typeof data12 == 'number' && isFinite(data12)) {
        if (data12 > 90 || isNaN(data12)) {
          const err62 = {
            instancePath: instancePath + '/adoption_silence_days',
            schemaPath: '#/properties/adoption_silence_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 90 },
          };
          if (vErrors === null) {
            vErrors = [err62];
          } else {
            vErrors.push(err62);
          }
          errors++;
        }
        if (data12 < 1 || isNaN(data12)) {
          const err63 = {
            instancePath: instancePath + '/adoption_silence_days',
            schemaPath: '#/properties/adoption_silence_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err63];
          } else {
            vErrors.push(err63);
          }
          errors++;
        }
      }
      var _valid12 = _errs76 === errors;
      valid13 = valid13 || _valid12;
      const _errs78 = errors;
      if (data12 !== null) {
        const err64 = {
          instancePath: instancePath + '/adoption_silence_days',
          schemaPath: '#/properties/adoption_silence_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err64];
        } else {
          vErrors.push(err64);
        }
        errors++;
      }
      var _valid12 = _errs78 === errors;
      valid13 = valid13 || _valid12;
      if (!valid13) {
        const err65 = {
          instancePath: instancePath + '/adoption_silence_days',
          schemaPath: '#/properties/adoption_silence_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err65];
        } else {
          vErrors.push(err65);
        }
        errors++;
      } else {
        errors = _errs75;
        if (vErrors !== null) {
          if (_errs75) {
            vErrors.length = _errs75;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.exposure_coverage_threshold_percent !== undefined) {
      let data13 = data.exposure_coverage_threshold_percent;
      const _errs81 = errors;
      let valid14 = false;
      const _errs82 = errors;
      if (!(typeof data13 == 'number' && !(data13 % 1) && !isNaN(data13) && isFinite(data13))) {
        const err66 = {
          instancePath: instancePath + '/exposure_coverage_threshold_percent',
          schemaPath: '#/properties/exposure_coverage_threshold_percent/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err66];
        } else {
          vErrors.push(err66);
        }
        errors++;
      }
      if (typeof data13 == 'number' && isFinite(data13)) {
        if (data13 > 100 || isNaN(data13)) {
          const err67 = {
            instancePath: instancePath + '/exposure_coverage_threshold_percent',
            schemaPath: '#/properties/exposure_coverage_threshold_percent/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 100 },
          };
          if (vErrors === null) {
            vErrors = [err67];
          } else {
            vErrors.push(err67);
          }
          errors++;
        }
        if (data13 < 0 || isNaN(data13)) {
          const err68 = {
            instancePath: instancePath + '/exposure_coverage_threshold_percent',
            schemaPath: '#/properties/exposure_coverage_threshold_percent/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 0 },
          };
          if (vErrors === null) {
            vErrors = [err68];
          } else {
            vErrors.push(err68);
          }
          errors++;
        }
      }
      var _valid13 = _errs82 === errors;
      valid14 = valid14 || _valid13;
      const _errs84 = errors;
      if (data13 !== null) {
        const err69 = {
          instancePath: instancePath + '/exposure_coverage_threshold_percent',
          schemaPath: '#/properties/exposure_coverage_threshold_percent/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err69];
        } else {
          vErrors.push(err69);
        }
        errors++;
      }
      var _valid13 = _errs84 === errors;
      valid14 = valid14 || _valid13;
      if (!valid14) {
        const err70 = {
          instancePath: instancePath + '/exposure_coverage_threshold_percent',
          schemaPath: '#/properties/exposure_coverage_threshold_percent/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err70];
        } else {
          vErrors.push(err70);
        }
        errors++;
      } else {
        errors = _errs81;
        if (vErrors !== null) {
          if (_errs81) {
            vErrors.length = _errs81;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.email_grouping_hours !== undefined) {
      let data14 = data.email_grouping_hours;
      const _errs87 = errors;
      let valid15 = false;
      const _errs88 = errors;
      if (!(typeof data14 == 'number' && !(data14 % 1) && !isNaN(data14) && isFinite(data14))) {
        const err71 = {
          instancePath: instancePath + '/email_grouping_hours',
          schemaPath: '#/properties/email_grouping_hours/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err71];
        } else {
          vErrors.push(err71);
        }
        errors++;
      }
      if (typeof data14 == 'number' && isFinite(data14)) {
        if (data14 > 24 || isNaN(data14)) {
          const err72 = {
            instancePath: instancePath + '/email_grouping_hours',
            schemaPath: '#/properties/email_grouping_hours/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 24 },
          };
          if (vErrors === null) {
            vErrors = [err72];
          } else {
            vErrors.push(err72);
          }
          errors++;
        }
        if (data14 < 1 || isNaN(data14)) {
          const err73 = {
            instancePath: instancePath + '/email_grouping_hours',
            schemaPath: '#/properties/email_grouping_hours/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err73];
          } else {
            vErrors.push(err73);
          }
          errors++;
        }
      }
      var _valid14 = _errs88 === errors;
      valid15 = valid15 || _valid14;
      const _errs90 = errors;
      if (data14 !== null) {
        const err74 = {
          instancePath: instancePath + '/email_grouping_hours',
          schemaPath: '#/properties/email_grouping_hours/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err74];
        } else {
          vErrors.push(err74);
        }
        errors++;
      }
      var _valid14 = _errs90 === errors;
      valid15 = valid15 || _valid14;
      if (!valid15) {
        const err75 = {
          instancePath: instancePath + '/email_grouping_hours',
          schemaPath: '#/properties/email_grouping_hours/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err75];
        } else {
          vErrors.push(err75);
        }
        errors++;
      } else {
        errors = _errs87;
        if (vErrors !== null) {
          if (_errs87) {
            vErrors.length = _errs87;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.export_max_days !== undefined) {
      let data15 = data.export_max_days;
      const _errs93 = errors;
      let valid16 = false;
      const _errs94 = errors;
      if (!(typeof data15 == 'number' && !(data15 % 1) && !isNaN(data15) && isFinite(data15))) {
        const err76 = {
          instancePath: instancePath + '/export_max_days',
          schemaPath: '#/properties/export_max_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err76];
        } else {
          vErrors.push(err76);
        }
        errors++;
      }
      if (typeof data15 == 'number' && isFinite(data15)) {
        if (data15 > 3660 || isNaN(data15)) {
          const err77 = {
            instancePath: instancePath + '/export_max_days',
            schemaPath: '#/properties/export_max_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 3660 },
          };
          if (vErrors === null) {
            vErrors = [err77];
          } else {
            vErrors.push(err77);
          }
          errors++;
        }
        if (data15 < 1 || isNaN(data15)) {
          const err78 = {
            instancePath: instancePath + '/export_max_days',
            schemaPath: '#/properties/export_max_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err78];
          } else {
            vErrors.push(err78);
          }
          errors++;
        }
      }
      var _valid15 = _errs94 === errors;
      valid16 = valid16 || _valid15;
      const _errs96 = errors;
      if (data15 !== null) {
        const err79 = {
          instancePath: instancePath + '/export_max_days',
          schemaPath: '#/properties/export_max_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err79];
        } else {
          vErrors.push(err79);
        }
        errors++;
      }
      var _valid15 = _errs96 === errors;
      valid16 = valid16 || _valid15;
      if (!valid16) {
        const err80 = {
          instancePath: instancePath + '/export_max_days',
          schemaPath: '#/properties/export_max_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err80];
        } else {
          vErrors.push(err80);
        }
        errors++;
      } else {
        errors = _errs93;
        if (vErrors !== null) {
          if (_errs93) {
            vErrors.length = _errs93;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.export_max_zones !== undefined) {
      let data16 = data.export_max_zones;
      const _errs99 = errors;
      let valid17 = false;
      const _errs100 = errors;
      if (!(typeof data16 == 'number' && !(data16 % 1) && !isNaN(data16) && isFinite(data16))) {
        const err81 = {
          instancePath: instancePath + '/export_max_zones',
          schemaPath: '#/properties/export_max_zones/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err81];
        } else {
          vErrors.push(err81);
        }
        errors++;
      }
      if (typeof data16 == 'number' && isFinite(data16)) {
        if (data16 > 500 || isNaN(data16)) {
          const err82 = {
            instancePath: instancePath + '/export_max_zones',
            schemaPath: '#/properties/export_max_zones/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 500 },
          };
          if (vErrors === null) {
            vErrors = [err82];
          } else {
            vErrors.push(err82);
          }
          errors++;
        }
        if (data16 < 1 || isNaN(data16)) {
          const err83 = {
            instancePath: instancePath + '/export_max_zones',
            schemaPath: '#/properties/export_max_zones/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err83];
          } else {
            vErrors.push(err83);
          }
          errors++;
        }
      }
      var _valid16 = _errs100 === errors;
      valid17 = valid17 || _valid16;
      const _errs102 = errors;
      if (data16 !== null) {
        const err84 = {
          instancePath: instancePath + '/export_max_zones',
          schemaPath: '#/properties/export_max_zones/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err84];
        } else {
          vErrors.push(err84);
        }
        errors++;
      }
      var _valid16 = _errs102 === errors;
      valid17 = valid17 || _valid16;
      if (!valid17) {
        const err85 = {
          instancePath: instancePath + '/export_max_zones',
          schemaPath: '#/properties/export_max_zones/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err85];
        } else {
          vErrors.push(err85);
        }
        errors++;
      } else {
        errors = _errs99;
        if (vErrors !== null) {
          if (_errs99) {
            vErrors.length = _errs99;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.single_occupancy_default_window_minutes !== undefined) {
      let data17 = data.single_occupancy_default_window_minutes;
      const _errs105 = errors;
      let valid18 = false;
      const _errs106 = errors;
      if (!(typeof data17 == 'number' && !(data17 % 1) && !isNaN(data17) && isFinite(data17))) {
        const err86 = {
          instancePath: instancePath + '/single_occupancy_default_window_minutes',
          schemaPath: '#/properties/single_occupancy_default_window_minutes/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err86];
        } else {
          vErrors.push(err86);
        }
        errors++;
      }
      if (typeof data17 == 'number' && isFinite(data17)) {
        if (data17 > 480 || isNaN(data17)) {
          const err87 = {
            instancePath: instancePath + '/single_occupancy_default_window_minutes',
            schemaPath: '#/properties/single_occupancy_default_window_minutes/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 480 },
          };
          if (vErrors === null) {
            vErrors = [err87];
          } else {
            vErrors.push(err87);
          }
          errors++;
        }
        if (data17 < 15 || isNaN(data17)) {
          const err88 = {
            instancePath: instancePath + '/single_occupancy_default_window_minutes',
            schemaPath: '#/properties/single_occupancy_default_window_minutes/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 15 },
          };
          if (vErrors === null) {
            vErrors = [err88];
          } else {
            vErrors.push(err88);
          }
          errors++;
        }
      }
      var _valid17 = _errs106 === errors;
      valid18 = valid18 || _valid17;
      const _errs108 = errors;
      if (data17 !== null) {
        const err89 = {
          instancePath: instancePath + '/single_occupancy_default_window_minutes',
          schemaPath: '#/properties/single_occupancy_default_window_minutes/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err89];
        } else {
          vErrors.push(err89);
        }
        errors++;
      }
      var _valid17 = _errs108 === errors;
      valid18 = valid18 || _valid17;
      if (!valid18) {
        const err90 = {
          instancePath: instancePath + '/single_occupancy_default_window_minutes',
          schemaPath: '#/properties/single_occupancy_default_window_minutes/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err90];
        } else {
          vErrors.push(err90);
        }
        errors++;
      } else {
        errors = _errs105;
        if (vErrors !== null) {
          if (_errs105) {
            vErrors.length = _errs105;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reminder_interval_hours !== undefined) {
      let data18 = data.reminder_interval_hours;
      const _errs111 = errors;
      let valid19 = false;
      const _errs112 = errors;
      if (!(typeof data18 == 'number' && !(data18 % 1) && !isNaN(data18) && isFinite(data18))) {
        const err91 = {
          instancePath: instancePath + '/reminder_interval_hours',
          schemaPath: '#/properties/reminder_interval_hours/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err91];
        } else {
          vErrors.push(err91);
        }
        errors++;
      }
      if (typeof data18 == 'number' && isFinite(data18)) {
        if (data18 > 168 || isNaN(data18)) {
          const err92 = {
            instancePath: instancePath + '/reminder_interval_hours',
            schemaPath: '#/properties/reminder_interval_hours/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 168 },
          };
          if (vErrors === null) {
            vErrors = [err92];
          } else {
            vErrors.push(err92);
          }
          errors++;
        }
        if (data18 < 1 || isNaN(data18)) {
          const err93 = {
            instancePath: instancePath + '/reminder_interval_hours',
            schemaPath: '#/properties/reminder_interval_hours/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err93];
          } else {
            vErrors.push(err93);
          }
          errors++;
        }
      }
      var _valid18 = _errs112 === errors;
      valid19 = valid19 || _valid18;
      const _errs114 = errors;
      if (data18 !== null) {
        const err94 = {
          instancePath: instancePath + '/reminder_interval_hours',
          schemaPath: '#/properties/reminder_interval_hours/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err94];
        } else {
          vErrors.push(err94);
        }
        errors++;
      }
      var _valid18 = _errs114 === errors;
      valid19 = valid19 || _valid18;
      if (!valid19) {
        const err95 = {
          instancePath: instancePath + '/reminder_interval_hours',
          schemaPath: '#/properties/reminder_interval_hours/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err95];
        } else {
          vErrors.push(err95);
        }
        errors++;
      } else {
        errors = _errs111;
        if (vErrors !== null) {
          if (_errs111) {
            vErrors.length = _errs111;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.business_calendar !== undefined) {
      let data19 = data.business_calendar;
      const _errs117 = errors;
      let valid20 = false;
      const _errs118 = errors;
      if (
        !validate89(data19, {
          instancePath: instancePath + '/business_calendar',
          parentData: data,
          parentDataProperty: 'business_calendar',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate89.errors : vErrors.concat(validate89.errors);
        errors = vErrors.length;
      }
      var _valid19 = _errs118 === errors;
      valid20 = valid20 || _valid19;
      const _errs119 = errors;
      if (data19 !== null) {
        const err96 = {
          instancePath: instancePath + '/business_calendar',
          schemaPath: '#/properties/business_calendar/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err96];
        } else {
          vErrors.push(err96);
        }
        errors++;
      }
      var _valid19 = _errs119 === errors;
      valid20 = valid20 || _valid19;
      if (!valid20) {
        const err97 = {
          instancePath: instancePath + '/business_calendar',
          schemaPath: '#/properties/business_calendar/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err97];
        } else {
          vErrors.push(err97);
        }
        errors++;
      } else {
        errors = _errs117;
        if (vErrors !== null) {
          if (_errs117) {
            vErrors.length = _errs117;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason !== undefined) {
      let data20 = data.reason;
      const _errs122 = errors;
      let valid21 = false;
      const _errs123 = errors;
      if (typeof data20 === 'string') {
        if (func1(data20) > 500) {
          const err98 = {
            instancePath: instancePath + '/reason',
            schemaPath: '#/properties/reason/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 500 },
          };
          if (vErrors === null) {
            vErrors = [err98];
          } else {
            vErrors.push(err98);
          }
          errors++;
        }
        if (func1(data20) < 10) {
          const err99 = {
            instancePath: instancePath + '/reason',
            schemaPath: '#/properties/reason/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 10 },
          };
          if (vErrors === null) {
            vErrors = [err99];
          } else {
            vErrors.push(err99);
          }
          errors++;
        }
      } else {
        const err100 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err100];
        } else {
          vErrors.push(err100);
        }
        errors++;
      }
      var _valid20 = _errs123 === errors;
      valid21 = valid21 || _valid20;
      const _errs125 = errors;
      if (data20 !== null) {
        const err101 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err101];
        } else {
          vErrors.push(err101);
        }
        errors++;
      }
      var _valid20 = _errs125 === errors;
      valid21 = valid21 || _valid20;
      if (!valid21) {
        const err102 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err102];
        } else {
          vErrors.push(err102);
        }
        errors++;
      } else {
        errors = _errs122;
        if (vErrors !== null) {
          if (_errs122) {
            vErrors.length = _errs122;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err103 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err103];
    } else {
      vErrors.push(err103);
    }
    errors++;
  }
  validate88.errors = vErrors;
  return errors === 0;
}
validate88.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate87(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate87.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate88(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate88.errors : vErrors.concat(validate88.errors);
    errors = vErrors.length;
  }
  validate87.errors = vErrors;
  return errors === 0;
}
validate87.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__change_password_auth_password_post = validate92;
const schema44 = { $ref: 'components.json#/$defs/PasswordChangeRequest' };
const schema45 = {
  properties: {
    current_password: { type: 'string', maxLength: 128, minLength: 1 },
    new_password: { type: 'string', maxLength: 1024, minLength: 1 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['current_password', 'new_password'],
};

function validate93(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate93.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.current_password === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'current_password' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.new_password === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'new_password' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'current_password' || key0 === 'new_password')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.current_password !== undefined) {
      let data0 = data.current_password;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err3 = {
            instancePath: instancePath + '/current_password',
            schemaPath: '#/properties/current_password/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err4 = {
            instancePath: instancePath + '/current_password',
            schemaPath: '#/properties/current_password/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/current_password',
          schemaPath: '#/properties/current_password/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.new_password !== undefined) {
      let data1 = data.new_password;
      if (typeof data1 === 'string') {
        if (func1(data1) > 1024) {
          const err6 = {
            instancePath: instancePath + '/new_password',
            schemaPath: '#/properties/new_password/maxLength',
            keyword: 'maxLength',
            params: { limit: 1024 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err7 = {
            instancePath: instancePath + '/new_password',
            schemaPath: '#/properties/new_password/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/new_password',
          schemaPath: '#/properties/new_password/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate93.errors = vErrors;
  return errors === 0;
}
validate93.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate92(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate92.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate93(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate93.errors : vErrors.concat(validate93.errors);
    errors = vErrors.length;
  }
  validate92.errors = vErrors;
  return errors === 0;
}
validate92.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__change_vocabulary_loop_vocabulary_changes_post = validate95;
const schema46 = { $ref: 'components.json#/$defs/VocabularyChangeBody' };
const schema47 = {
  properties: {
    added: { items: { $ref: 'components.json#/$defs/TermBody' }, type: 'array', maxItems: 200 },
    removed: { items: { type: 'string', format: 'uuid' }, type: 'array', maxItems: 200 },
    declared_names_added: {
      items: { $ref: 'components.json#/$defs/DeclaredNameBody' },
      type: 'array',
      maxItems: 200,
    },
    declared_names_removed: {
      items: { type: 'string', format: 'uuid' },
      type: 'array',
      maxItems: 200,
    },
    reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};
const schema48 = {
  properties: {
    category: { type: 'string' },
    form: { type: 'string' },
    value: { type: 'string' },
    suggestion_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['category', 'form', 'value', 'suggestion_es'],
};

function validate97(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate97.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.category === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'category' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.form === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'form' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.value === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'value' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.suggestion_es === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'suggestion_es' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'category' ||
        key0 === 'form' ||
        key0 === 'value' ||
        key0 === 'suggestion_es'
      )) {
        const err4 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.category !== undefined) {
      if (typeof data.category !== 'string') {
        const err5 = {
          instancePath: instancePath + '/category',
          schemaPath: '#/properties/category/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.form !== undefined) {
      if (typeof data.form !== 'string') {
        const err6 = {
          instancePath: instancePath + '/form',
          schemaPath: '#/properties/form/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.value !== undefined) {
      if (typeof data.value !== 'string') {
        const err7 = {
          instancePath: instancePath + '/value',
          schemaPath: '#/properties/value/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.suggestion_es !== undefined) {
      if (typeof data.suggestion_es !== 'string') {
        const err8 = {
          instancePath: instancePath + '/suggestion_es',
          schemaPath: '#/properties/suggestion_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate97.errors = vErrors;
  return errors === 0;
}
validate97.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema49 = {
  properties: { kind: { type: 'string' }, value: { type: 'string' } },
  additionalProperties: false,
  type: 'object',
  required: ['kind', 'value'],
};

function validate99(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate99.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.kind === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'kind' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.value === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'value' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'kind' || key0 === 'value')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.kind !== undefined) {
      if (typeof data.kind !== 'string') {
        const err3 = {
          instancePath: instancePath + '/kind',
          schemaPath: '#/properties/kind/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.value !== undefined) {
      if (typeof data.value !== 'string') {
        const err4 = {
          instancePath: instancePath + '/value',
          schemaPath: '#/properties/value/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate99.errors = vErrors;
  return errors === 0;
}
validate99.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate96(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate96.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'added' ||
        key0 === 'removed' ||
        key0 === 'declared_names_added' ||
        key0 === 'declared_names_removed' ||
        key0 === 'reason'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.added !== undefined) {
      let data0 = data.added;
      if (Array.isArray(data0)) {
        if (data0.length > 200) {
          const err1 = {
            instancePath: instancePath + '/added',
            schemaPath: '#/properties/added/maxItems',
            keyword: 'maxItems',
            params: { limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate97(data0[i0], {
              instancePath: instancePath + '/added/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate97.errors : vErrors.concat(validate97.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/added',
          schemaPath: '#/properties/added/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.removed !== undefined) {
      let data2 = data.removed;
      if (Array.isArray(data2)) {
        if (data2.length > 200) {
          const err3 = {
            instancePath: instancePath + '/removed',
            schemaPath: '#/properties/removed/maxItems',
            keyword: 'maxItems',
            params: { limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len1 = data2.length;
        for (let i1 = 0; i1 < len1; i1++) {
          let data3 = data2[i1];
          if (typeof data3 === 'string') {
            if (!formats0.test(data3)) {
              const err4 = {
                instancePath: instancePath + '/removed/' + i1,
                schemaPath: '#/properties/removed/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err4];
              } else {
                vErrors.push(err4);
              }
              errors++;
            }
          } else {
            const err5 = {
              instancePath: instancePath + '/removed/' + i1,
              schemaPath: '#/properties/removed/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err5];
            } else {
              vErrors.push(err5);
            }
            errors++;
          }
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/removed',
          schemaPath: '#/properties/removed/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.declared_names_added !== undefined) {
      let data4 = data.declared_names_added;
      if (Array.isArray(data4)) {
        if (data4.length > 200) {
          const err7 = {
            instancePath: instancePath + '/declared_names_added',
            schemaPath: '#/properties/declared_names_added/maxItems',
            keyword: 'maxItems',
            params: { limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        const len2 = data4.length;
        for (let i2 = 0; i2 < len2; i2++) {
          if (
            !validate99(data4[i2], {
              instancePath: instancePath + '/declared_names_added/' + i2,
              parentData: data4,
              parentDataProperty: i2,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate99.errors : vErrors.concat(validate99.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/declared_names_added',
          schemaPath: '#/properties/declared_names_added/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.declared_names_removed !== undefined) {
      let data6 = data.declared_names_removed;
      if (Array.isArray(data6)) {
        if (data6.length > 200) {
          const err9 = {
            instancePath: instancePath + '/declared_names_removed',
            schemaPath: '#/properties/declared_names_removed/maxItems',
            keyword: 'maxItems',
            params: { limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
        const len3 = data6.length;
        for (let i3 = 0; i3 < len3; i3++) {
          let data7 = data6[i3];
          if (typeof data7 === 'string') {
            if (!formats0.test(data7)) {
              const err10 = {
                instancePath: instancePath + '/declared_names_removed/' + i3,
                schemaPath: '#/properties/declared_names_removed/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err10];
              } else {
                vErrors.push(err10);
              }
              errors++;
            }
          } else {
            const err11 = {
              instancePath: instancePath + '/declared_names_removed/' + i3,
              schemaPath: '#/properties/declared_names_removed/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err11];
            } else {
              vErrors.push(err11);
            }
            errors++;
          }
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/declared_names_removed',
          schemaPath: '#/properties/declared_names_removed/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
    }
    if (data.reason !== undefined) {
      let data8 = data.reason;
      const _errs17 = errors;
      let valid9 = false;
      const _errs18 = errors;
      if (typeof data8 !== 'string') {
        const err13 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid0 = _errs18 === errors;
      valid9 = valid9 || _valid0;
      const _errs20 = errors;
      if (data8 !== null) {
        const err14 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid0 = _errs20 === errors;
      valid9 = valid9 || _valid0;
      if (!valid9) {
        const err15 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      } else {
        errors = _errs17;
        if (vErrors !== null) {
          if (_errs17) {
            vErrors.length = _errs17;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err16 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err16];
    } else {
      vErrors.push(err16);
    }
    errors++;
  }
  validate96.errors = vErrors;
  return errors === 0;
}
validate96.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate95(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate95.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate96(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate96.errors : vErrors.concat(validate96.errors);
    errors = vErrors.length;
  }
  validate95.errors = vErrors;
  return errors === 0;
}
validate95.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__classify_loop_findings__finding_state_id__classifications_post = validate102;
const schema50 = { $ref: 'components.json#/$defs/ClassificationBody' };
const schema51 = {
  properties: {
    outcome: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    reason_category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    reason_text: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    reinforced_reason: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
};

function validate103(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate103.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'outcome' ||
        key0 === 'reason_category' ||
        key0 === 'reason_text' ||
        key0 === 'reinforced_reason'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.outcome !== undefined) {
      let data0 = data.outcome;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_category !== undefined) {
      let data1 = data.reason_category;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_text !== undefined) {
      let data2 = data.reason_text;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (typeof data2 !== 'string') {
        const err7 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err8 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err9 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reinforced_reason !== undefined) {
      if (typeof data.reinforced_reason !== 'boolean') {
        const err10 = {
          instancePath: instancePath + '/reinforced_reason',
          schemaPath: '#/properties/reinforced_reason/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
  } else {
    const err11 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err11];
    } else {
      vErrors.push(err11);
    }
    errors++;
  }
  validate103.errors = vErrors;
  return errors === 0;
}
validate103.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate102(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate102.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate103(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate103.errors : vErrors.concat(validate103.errors);
    errors = vErrors.length;
  }
  validate102.errors = vErrors;
  return errors === 0;
}
validate102.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__close_loop_findings__finding_state_id__closure_post = validate105;
const schema52 = { $ref: 'components.json#/$defs/ClosureBody' };
const schema53 = {
  properties: {
    closure_description: { type: 'string', maxLength: 2000, minLength: 20 },
    verification_note: {
      anyOf: [{ type: 'string', maxLength: 1000, minLength: 1 }, { type: 'null' }],
    },
    professional_license: { anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['closure_description'],
};

function validate106(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate106.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.closure_description === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'closure_description' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'closure_description' ||
        key0 === 'verification_note' ||
        key0 === 'professional_license'
      )) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.closure_description !== undefined) {
      let data0 = data.closure_description;
      if (typeof data0 === 'string') {
        if (func1(data0) > 2000) {
          const err2 = {
            instancePath: instancePath + '/closure_description',
            schemaPath: '#/properties/closure_description/maxLength',
            keyword: 'maxLength',
            params: { limit: 2000 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        if (func1(data0) < 20) {
          const err3 = {
            instancePath: instancePath + '/closure_description',
            schemaPath: '#/properties/closure_description/minLength',
            keyword: 'minLength',
            params: { limit: 20 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/closure_description',
          schemaPath: '#/properties/closure_description/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.verification_note !== undefined) {
      let data1 = data.verification_note;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (typeof data1 === 'string') {
        if (func1(data1) > 1000) {
          const err5 = {
            instancePath: instancePath + '/verification_note',
            schemaPath: '#/properties/verification_note/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err6 = {
            instancePath: instancePath + '/verification_note',
            schemaPath: '#/properties/verification_note/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/verification_note',
          schemaPath: '#/properties/verification_note/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data1 !== null) {
        const err8 = {
          instancePath: instancePath + '/verification_note',
          schemaPath: '#/properties/verification_note/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err9 = {
          instancePath: instancePath + '/verification_note',
          schemaPath: '#/properties/verification_note/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.professional_license !== undefined) {
      let data2 = data.professional_license;
      const _errs11 = errors;
      let valid2 = false;
      const _errs12 = errors;
      if (typeof data2 === 'string') {
        if (func1(data2) > 64) {
          const err10 = {
            instancePath: instancePath + '/professional_license',
            schemaPath: '#/properties/professional_license/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      const _errs14 = errors;
      if (data2 !== null) {
        const err12 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err13 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err14 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err14];
    } else {
      vErrors.push(err14);
    }
    errors++;
  }
  validate106.errors = vErrors;
  return errors === 0;
}
validate106.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate105(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate105.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate106(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate106.errors : vErrors.concat(validate106.errors);
    errors = vErrors.length;
  }
  validate105.errors = vErrors;
  return errors === 0;
}
validate105.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__close_step_walk_tests__session_id__steps__step_id__close_post = validate108;
const schema54 = { $ref: 'components.json#/$defs/CloseStepBody' };
const schema55 = {
  properties: {
    correction: { anyOf: [{ $ref: 'components.json#/$defs/CorrectionBody' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};
const schema56 = {
  properties: {
    started_at: { anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }] },
    ended_at: { anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }] },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es'],
};
const formats16 = runtime1.fullFormats['date-time'];

function validate110(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate110.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'started_at' || key0 === 'ended_at' || key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.started_at !== undefined) {
      let data0 = data.started_at;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (!formats16.validate(data0)) {
          const err2 = {
            instancePath: instancePath + '/started_at',
            schemaPath: '#/properties/started_at/anyOf/0/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/started_at',
          schemaPath: '#/properties/started_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err4 = {
          instancePath: instancePath + '/started_at',
          schemaPath: '#/properties/started_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/started_at',
          schemaPath: '#/properties/started_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.ended_at !== undefined) {
      let data1 = data.ended_at;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err6 = {
            instancePath: instancePath + '/ended_at',
            schemaPath: '#/properties/ended_at/anyOf/0/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/ended_at',
          schemaPath: '#/properties/ended_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err8 = {
          instancePath: instancePath + '/ended_at',
          schemaPath: '#/properties/ended_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err9 = {
          instancePath: instancePath + '/ended_at',
          schemaPath: '#/properties/ended_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err10 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
  } else {
    const err11 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err11];
    } else {
      vErrors.push(err11);
    }
    errors++;
  }
  validate110.errors = vErrors;
  return errors === 0;
}
validate110.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate109(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate109.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'correction')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.correction !== undefined) {
      let data0 = data.correction;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (
        !validate110(data0, {
          instancePath: instancePath + '/correction',
          parentData: data,
          parentDataProperty: 'correction',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate110.errors : vErrors.concat(validate110.errors);
        errors = vErrors.length;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs5 = errors;
      if (data0 !== null) {
        const err1 = {
          instancePath: instancePath + '/correction',
          schemaPath: '#/properties/correction/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs5 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err2 = {
          instancePath: instancePath + '/correction',
          schemaPath: '#/properties/correction/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate109.errors = vErrors;
  return errors === 0;
}
validate109.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate108(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate108.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate109(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate109.errors : vErrors.concat(validate109.errors);
    errors = vErrors.length;
  }
  validate108.errors = vErrors;
  return errors === 0;
}
validate108.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__close_walk_test_walk_tests__session_id__close_post = validate113;
const schema57 = { $ref: 'components.json#/$defs/CloseBody' };
const schema58 = {
  properties: {
    signatures: {
      items: { $ref: 'components.json#/$defs/SignatureBody' },
      type: 'array',
      maxItems: 32,
      minItems: 1,
    },
    false_alarm_acceptance: {
      anyOf: [{ $ref: 'components.json#/$defs/FalseAlarmAcceptanceBody' }, { type: 'null' }],
    },
    installer_measurements: { $ref: 'components.json#/$defs/InstallerMeasurementsBody' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['signatures', 'installer_measurements'],
};
const schema59 = {
  properties: { user_id: { type: 'string', format: 'uuid' } },
  additionalProperties: false,
  type: 'object',
  required: ['user_id'],
};

function validate115(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate115.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.user_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'user_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'user_id')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.user_id !== undefined) {
      let data0 = data.user_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err2 = {
            instancePath: instancePath + '/user_id',
            schemaPath: '#/properties/user_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/user_id',
          schemaPath: '#/properties/user_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
  } else {
    const err4 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err4];
    } else {
      vErrors.push(err4);
    }
    errors++;
  }
  validate115.errors = vErrors;
  return errors === 0;
}
validate115.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema60 = {
  properties: { reason_es: { type: 'string' } },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es'],
};

function validate117(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate117.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err2 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate117.errors = vErrors;
  return errors === 0;
}
validate117.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema61 = {
  properties: {
    beacon_latency_ms_p95: { type: 'integer' },
    baselines: {
      items: { $ref: 'components.json#/$defs/BaselineBody' },
      type: 'array',
      maxItems: 8,
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['beacon_latency_ms_p95'],
};
const schema62 = {
  properties: {
    camera_id: { type: 'string', format: 'uuid' },
    zone_id: { type: 'string', format: 'uuid' },
    captured_at: { type: 'string', format: 'date-time' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['camera_id', 'zone_id', 'captured_at'],
};

function validate120(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate120.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.camera_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'camera_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.zone_id === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_id' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.captured_at === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'captured_at' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'camera_id' || key0 === 'zone_id' || key0 === 'captured_at')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.camera_id !== undefined) {
      let data0 = data.camera_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err4 = {
            instancePath: instancePath + '/camera_id',
            schemaPath: '#/properties/camera_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/camera_id',
          schemaPath: '#/properties/camera_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data1 = data.zone_id;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err6 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.captured_at !== undefined) {
      let data2 = data.captured_at;
      if (typeof data2 === 'string') {
        if (!formats16.validate(data2)) {
          const err8 = {
            instancePath: instancePath + '/captured_at',
            schemaPath: '#/properties/captured_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/captured_at',
          schemaPath: '#/properties/captured_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
  } else {
    const err10 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err10];
    } else {
      vErrors.push(err10);
    }
    errors++;
  }
  validate120.errors = vErrors;
  return errors === 0;
}
validate120.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate119(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate119.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.beacon_latency_ms_p95 === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'beacon_latency_ms_p95' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'beacon_latency_ms_p95' || key0 === 'baselines')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.beacon_latency_ms_p95 !== undefined) {
      let data0 = data.beacon_latency_ms_p95;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err2 = {
          instancePath: instancePath + '/beacon_latency_ms_p95',
          schemaPath: '#/properties/beacon_latency_ms_p95/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.baselines !== undefined) {
      let data1 = data.baselines;
      if (Array.isArray(data1)) {
        if (data1.length > 8) {
          const err3 = {
            instancePath: instancePath + '/baselines',
            schemaPath: '#/properties/baselines/maxItems',
            keyword: 'maxItems',
            params: { limit: 8 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate120(data1[i0], {
              instancePath: instancePath + '/baselines/' + i0,
              parentData: data1,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate120.errors : vErrors.concat(validate120.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/baselines',
          schemaPath: '#/properties/baselines/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate119.errors = vErrors;
  return errors === 0;
}
validate119.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate114(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate114.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.signatures === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'signatures' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.installer_measurements === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'installer_measurements' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'signatures' ||
        key0 === 'false_alarm_acceptance' ||
        key0 === 'installer_measurements'
      )) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.signatures !== undefined) {
      let data0 = data.signatures;
      if (Array.isArray(data0)) {
        if (data0.length > 32) {
          const err3 = {
            instancePath: instancePath + '/signatures',
            schemaPath: '#/properties/signatures/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (data0.length < 1) {
          const err4 = {
            instancePath: instancePath + '/signatures',
            schemaPath: '#/properties/signatures/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate115(data0[i0], {
              instancePath: instancePath + '/signatures/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate115.errors : vErrors.concat(validate115.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/signatures',
          schemaPath: '#/properties/signatures/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.false_alarm_acceptance !== undefined) {
      let data2 = data.false_alarm_acceptance;
      const _errs6 = errors;
      let valid3 = false;
      const _errs7 = errors;
      if (
        !validate117(data2, {
          instancePath: instancePath + '/false_alarm_acceptance',
          parentData: data,
          parentDataProperty: 'false_alarm_acceptance',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate117.errors : vErrors.concat(validate117.errors);
        errors = vErrors.length;
      }
      var _valid0 = _errs7 === errors;
      valid3 = valid3 || _valid0;
      const _errs8 = errors;
      if (data2 !== null) {
        const err6 = {
          instancePath: instancePath + '/false_alarm_acceptance',
          schemaPath: '#/properties/false_alarm_acceptance/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid3 = valid3 || _valid0;
      if (!valid3) {
        const err7 = {
          instancePath: instancePath + '/false_alarm_acceptance',
          schemaPath: '#/properties/false_alarm_acceptance/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      } else {
        errors = _errs6;
        if (vErrors !== null) {
          if (_errs6) {
            vErrors.length = _errs6;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.installer_measurements !== undefined) {
      if (
        !validate119(data.installer_measurements, {
          instancePath: instancePath + '/installer_measurements',
          parentData: data,
          parentDataProperty: 'installer_measurements',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate119.errors : vErrors.concat(validate119.errors);
        errors = vErrors.length;
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate114.errors = vErrors;
  return errors === 0;
}
validate114.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate113(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate113.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate114(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate114.errors : vErrors.concat(validate114.errors);
    errors = vErrors.length;
  }
  validate113.errors = vErrors;
  return errors === 0;
}
validate113.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__commissioning_clips_zones__zone_id__commissioning_clips_get = validate124;
const schema63 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 36 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate124(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate124.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'after' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 36) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 36 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate124.errors = vErrors;
  return errors === 0;
}
validate124.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__create_agreement_zones__zone_id__use_agreements_post = validate125;
const schema64 = { $ref: 'components.json#/$defs/UseAgreementBody' };
const schema65 = {
  properties: {
    signatories: {
      items: { $ref: 'components.json#/$defs/SignatoryBody' },
      type: 'array',
      maxItems: 32,
    },
    document_ref: { anyOf: [{ additionalProperties: true, type: 'object' }, { type: 'null' }] },
    replaces_agreement_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['signatories'],
};
const schema66 = {
  properties: {
    role: { $ref: 'components.json#/$defs/Role' },
    user_id: { type: 'string', format: 'uuid' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['role', 'user_id'],
};

function validate127(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate127.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.role === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'role' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.user_id === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'user_id' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'role' || key0 === 'user_id')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.role !== undefined) {
      if (
        !validate71(data.role, {
          instancePath: instancePath + '/role',
          parentData: data,
          parentDataProperty: 'role',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate71.errors : vErrors.concat(validate71.errors);
        errors = vErrors.length;
      }
    }
    if (data.user_id !== undefined) {
      let data1 = data.user_id;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err3 = {
            instancePath: instancePath + '/user_id',
            schemaPath: '#/properties/user_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/user_id',
          schemaPath: '#/properties/user_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate127.errors = vErrors;
  return errors === 0;
}
validate127.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate126(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate126.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.signatories === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'signatories' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'signatories' ||
        key0 === 'document_ref' ||
        key0 === 'replaces_agreement_id'
      )) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.signatories !== undefined) {
      let data0 = data.signatories;
      if (Array.isArray(data0)) {
        if (data0.length > 32) {
          const err2 = {
            instancePath: instancePath + '/signatories',
            schemaPath: '#/properties/signatories/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate127(data0[i0], {
              instancePath: instancePath + '/signatories/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate127.errors : vErrors.concat(validate127.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/signatories',
          schemaPath: '#/properties/signatories/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.document_ref !== undefined) {
      let data2 = data.document_ref;
      const _errs6 = errors;
      let valid3 = false;
      const _errs7 = errors;
      if (data2 && typeof data2 == 'object' && !Array.isArray(data2)) {
      } else {
        const err4 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/anyOf/0/type',
          keyword: 'type',
          params: { type: 'object' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs7 === errors;
      valid3 = valid3 || _valid0;
      const _errs10 = errors;
      if (data2 !== null) {
        const err5 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid3 = valid3 || _valid0;
      if (!valid3) {
        const err6 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs6;
        if (vErrors !== null) {
          if (_errs6) {
            vErrors.length = _errs6;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.replaces_agreement_id !== undefined) {
      let data3 = data.replaces_agreement_id;
      const _errs13 = errors;
      let valid4 = false;
      const _errs14 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err7 = {
            instancePath: instancePath + '/replaces_agreement_id',
            schemaPath: '#/properties/replaces_agreement_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/replaces_agreement_id',
          schemaPath: '#/properties/replaces_agreement_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid4 = valid4 || _valid1;
      const _errs16 = errors;
      if (data3 !== null) {
        const err9 = {
          instancePath: instancePath + '/replaces_agreement_id',
          schemaPath: '#/properties/replaces_agreement_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs16 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err10 = {
          instancePath: instancePath + '/replaces_agreement_id',
          schemaPath: '#/properties/replaces_agreement_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err11 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err11];
    } else {
      vErrors.push(err11);
    }
    errors++;
  }
  validate126.errors = vErrors;
  return errors === 0;
}
validate126.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate125(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate125.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate126(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate126.errors : vErrors.concat(validate126.errors);
    errors = vErrors.length;
  }
  validate125.errors = vErrors;
  return errors === 0;
}
validate125.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__create_plant_plants_post = validate131;
const schema67 = { $ref: 'components.json#/$defs/PlantBody' };
const schema68 = {
  properties: {
    code: { type: 'string', maxLength: 32, minLength: 2 },
    name: { type: 'string', maxLength: 120, minLength: 1 },
    country: { type: 'string', maxLength: 2, minLength: 2 },
    data_region: { type: 'string', maxLength: 32, minLength: 1 },
    timezone: { type: 'string', maxLength: 64, minLength: 3 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['code', 'name', 'country', 'data_region', 'timezone'],
};

function validate132(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate132.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.code === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'code' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.name === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'name' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.country === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'country' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.data_region === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'data_region' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.timezone === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'timezone' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'code' ||
        key0 === 'name' ||
        key0 === 'country' ||
        key0 === 'data_region' ||
        key0 === 'timezone'
      )) {
        const err5 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.code !== undefined) {
      let data0 = data.code;
      if (typeof data0 === 'string') {
        if (func1(data0) > 32) {
          const err6 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/maxLength',
            keyword: 'maxLength',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data0) < 2) {
          const err7 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/minLength',
            keyword: 'minLength',
            params: { limit: 2 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.name !== undefined) {
      let data1 = data.name;
      if (typeof data1 === 'string') {
        if (func1(data1) > 120) {
          const err9 = {
            instancePath: instancePath + '/name',
            schemaPath: '#/properties/name/maxLength',
            keyword: 'maxLength',
            params: { limit: 120 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err10 = {
            instancePath: instancePath + '/name',
            schemaPath: '#/properties/name/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/name',
          schemaPath: '#/properties/name/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
    }
    if (data.country !== undefined) {
      let data2 = data.country;
      if (typeof data2 === 'string') {
        if (func1(data2) > 2) {
          const err12 = {
            instancePath: instancePath + '/country',
            schemaPath: '#/properties/country/maxLength',
            keyword: 'maxLength',
            params: { limit: 2 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
        if (func1(data2) < 2) {
          const err13 = {
            instancePath: instancePath + '/country',
            schemaPath: '#/properties/country/minLength',
            keyword: 'minLength',
            params: { limit: 2 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/country',
          schemaPath: '#/properties/country/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
    }
    if (data.data_region !== undefined) {
      let data3 = data.data_region;
      if (typeof data3 === 'string') {
        if (func1(data3) > 32) {
          const err15 = {
            instancePath: instancePath + '/data_region',
            schemaPath: '#/properties/data_region/maxLength',
            keyword: 'maxLength',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
        if (func1(data3) < 1) {
          const err16 = {
            instancePath: instancePath + '/data_region',
            schemaPath: '#/properties/data_region/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err16];
          } else {
            vErrors.push(err16);
          }
          errors++;
        }
      } else {
        const err17 = {
          instancePath: instancePath + '/data_region',
          schemaPath: '#/properties/data_region/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
    }
    if (data.timezone !== undefined) {
      let data4 = data.timezone;
      if (typeof data4 === 'string') {
        if (func1(data4) > 64) {
          const err18 = {
            instancePath: instancePath + '/timezone',
            schemaPath: '#/properties/timezone/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err18];
          } else {
            vErrors.push(err18);
          }
          errors++;
        }
        if (func1(data4) < 3) {
          const err19 = {
            instancePath: instancePath + '/timezone',
            schemaPath: '#/properties/timezone/minLength',
            keyword: 'minLength',
            params: { limit: 3 },
          };
          if (vErrors === null) {
            vErrors = [err19];
          } else {
            vErrors.push(err19);
          }
          errors++;
        }
      } else {
        const err20 = {
          instancePath: instancePath + '/timezone',
          schemaPath: '#/properties/timezone/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
    }
  } else {
    const err21 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err21];
    } else {
      vErrors.push(err21);
    }
    errors++;
  }
  validate132.errors = vErrors;
  return errors === 0;
}
validate132.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate131(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate131.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate132(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate132.errors : vErrors.concat(validate132.errors);
    errors = vErrors.length;
  }
  validate131.errors = vErrors;
  return errors === 0;
}
validate131.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__create_zone_plants__plant_id__zones_post = validate134;
const schema69 = { $ref: 'components.json#/$defs/ZoneBody' };
const schema70 = {
  properties: {
    code: { type: 'string', maxLength: 32, minLength: 2 },
    name: { type: 'string', maxLength: 120, minLength: 1 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['code', 'name'],
};

function validate135(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate135.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.code === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'code' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.name === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'name' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'code' || key0 === 'name')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.code !== undefined) {
      let data0 = data.code;
      if (typeof data0 === 'string') {
        if (func1(data0) > 32) {
          const err3 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/maxLength',
            keyword: 'maxLength',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (func1(data0) < 2) {
          const err4 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/minLength',
            keyword: 'minLength',
            params: { limit: 2 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.name !== undefined) {
      let data1 = data.name;
      if (typeof data1 === 'string') {
        if (func1(data1) > 120) {
          const err6 = {
            instancePath: instancePath + '/name',
            schemaPath: '#/properties/name/maxLength',
            keyword: 'maxLength',
            params: { limit: 120 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err7 = {
            instancePath: instancePath + '/name',
            schemaPath: '#/properties/name/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/name',
          schemaPath: '#/properties/name/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate135.errors = vErrors;
  return errors === 0;
}
validate135.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate134(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate134.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate135(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate135.errors : vErrors.concat(validate135.errors);
    errors = vErrors.length;
  }
  validate134.errors = vErrors;
  return errors === 0;
}
validate134.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__declare_loop_findings__finding_state_id__unclosable_post = validate137;
const schema71 = { $ref: 'components.json#/$defs/UnclosableDeclarationBody' };
const schema72 = {
  properties: {
    evaluated_alternative: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    rejection_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    responsible_user_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    decision_date: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    compensatory_measure: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    review_due_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    professional_license: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};

function validate138(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate138.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'evaluated_alternative' ||
        key0 === 'rejection_reason' ||
        key0 === 'responsible_user_id' ||
        key0 === 'decision_date' ||
        key0 === 'compensatory_measure' ||
        key0 === 'review_due_at' ||
        key0 === 'professional_license'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.evaluated_alternative !== undefined) {
      let data0 = data.evaluated_alternative;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/evaluated_alternative',
          schemaPath: '#/properties/evaluated_alternative/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/evaluated_alternative',
          schemaPath: '#/properties/evaluated_alternative/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/evaluated_alternative',
          schemaPath: '#/properties/evaluated_alternative/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.rejection_reason !== undefined) {
      let data1 = data.rejection_reason;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/rejection_reason',
          schemaPath: '#/properties/rejection_reason/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/rejection_reason',
          schemaPath: '#/properties/rejection_reason/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/rejection_reason',
          schemaPath: '#/properties/rejection_reason/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.responsible_user_id !== undefined) {
      let data2 = data.responsible_user_id;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err7 = {
            instancePath: instancePath + '/responsible_user_id',
            schemaPath: '#/properties/responsible_user_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/responsible_user_id',
          schemaPath: '#/properties/responsible_user_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err9 = {
          instancePath: instancePath + '/responsible_user_id',
          schemaPath: '#/properties/responsible_user_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err10 = {
          instancePath: instancePath + '/responsible_user_id',
          schemaPath: '#/properties/responsible_user_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.decision_date !== undefined) {
      let data3 = data.decision_date;
      const _errs21 = errors;
      let valid4 = false;
      const _errs22 = errors;
      if (typeof data3 !== 'string') {
        const err11 = {
          instancePath: instancePath + '/decision_date',
          schemaPath: '#/properties/decision_date/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid3 = _errs22 === errors;
      valid4 = valid4 || _valid3;
      const _errs24 = errors;
      if (data3 !== null) {
        const err12 = {
          instancePath: instancePath + '/decision_date',
          schemaPath: '#/properties/decision_date/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err13 = {
          instancePath: instancePath + '/decision_date',
          schemaPath: '#/properties/decision_date/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.compensatory_measure !== undefined) {
      let data4 = data.compensatory_measure;
      const _errs27 = errors;
      let valid5 = false;
      const _errs28 = errors;
      if (typeof data4 !== 'string') {
        const err14 = {
          instancePath: instancePath + '/compensatory_measure',
          schemaPath: '#/properties/compensatory_measure/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid4 = _errs28 === errors;
      valid5 = valid5 || _valid4;
      const _errs30 = errors;
      if (data4 !== null) {
        const err15 = {
          instancePath: instancePath + '/compensatory_measure',
          schemaPath: '#/properties/compensatory_measure/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid5 = valid5 || _valid4;
      if (!valid5) {
        const err16 = {
          instancePath: instancePath + '/compensatory_measure',
          schemaPath: '#/properties/compensatory_measure/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      } else {
        errors = _errs27;
        if (vErrors !== null) {
          if (_errs27) {
            vErrors.length = _errs27;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.review_due_at !== undefined) {
      let data5 = data.review_due_at;
      const _errs33 = errors;
      let valid6 = false;
      const _errs34 = errors;
      if (typeof data5 !== 'string') {
        const err17 = {
          instancePath: instancePath + '/review_due_at',
          schemaPath: '#/properties/review_due_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      var _valid5 = _errs34 === errors;
      valid6 = valid6 || _valid5;
      const _errs36 = errors;
      if (data5 !== null) {
        const err18 = {
          instancePath: instancePath + '/review_due_at',
          schemaPath: '#/properties/review_due_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      }
      var _valid5 = _errs36 === errors;
      valid6 = valid6 || _valid5;
      if (!valid6) {
        const err19 = {
          instancePath: instancePath + '/review_due_at',
          schemaPath: '#/properties/review_due_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      } else {
        errors = _errs33;
        if (vErrors !== null) {
          if (_errs33) {
            vErrors.length = _errs33;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.professional_license !== undefined) {
      let data6 = data.professional_license;
      const _errs39 = errors;
      let valid7 = false;
      const _errs40 = errors;
      if (typeof data6 !== 'string') {
        const err20 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid6 = _errs40 === errors;
      valid7 = valid7 || _valid6;
      const _errs42 = errors;
      if (data6 !== null) {
        const err21 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      }
      var _valid6 = _errs42 === errors;
      valid7 = valid7 || _valid6;
      if (!valid7) {
        const err22 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err22];
        } else {
          vErrors.push(err22);
        }
        errors++;
      } else {
        errors = _errs39;
        if (vErrors !== null) {
          if (_errs39) {
            vErrors.length = _errs39;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err23 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err23];
    } else {
      vErrors.push(err23);
    }
    errors++;
  }
  validate138.errors = vErrors;
  return errors === 0;
}
validate138.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate137(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate137.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate138(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate138.errors : vErrors.concat(validate138.errors);
    errors = vErrors.length;
  }
  validate137.errors = vErrors;
  return errors === 0;
}
validate137.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__declare_node_plants__plant_id__nodes_post = validate140;
const schema73 = { $ref: 'components.json#/$defs/NodeDeclarationBody' };
const schema74 = {
  properties: {
    code: { type: 'string' },
    zone_ids: { items: { type: 'string', format: 'uuid' }, type: 'array', maxItems: 16 },
    replaces_node_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['code', 'zone_ids'],
};

function validate141(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate141.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.code === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'code' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.zone_ids === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_ids' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'code' || key0 === 'zone_ids' || key0 === 'replaces_node_id')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.code !== undefined) {
      if (typeof data.code !== 'string') {
        const err3 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.zone_ids !== undefined) {
      let data1 = data.zone_ids;
      if (Array.isArray(data1)) {
        if (data1.length > 16) {
          const err4 = {
            instancePath: instancePath + '/zone_ids',
            schemaPath: '#/properties/zone_ids/maxItems',
            keyword: 'maxItems',
            params: { limit: 16 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data2 = data1[i0];
          if (typeof data2 === 'string') {
            if (!formats0.test(data2)) {
              const err5 = {
                instancePath: instancePath + '/zone_ids/' + i0,
                schemaPath: '#/properties/zone_ids/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err5];
              } else {
                vErrors.push(err5);
              }
              errors++;
            }
          } else {
            const err6 = {
              instancePath: instancePath + '/zone_ids/' + i0,
              schemaPath: '#/properties/zone_ids/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err6];
            } else {
              vErrors.push(err6);
            }
            errors++;
          }
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/zone_ids',
          schemaPath: '#/properties/zone_ids/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.replaces_node_id !== undefined) {
      let data3 = data.replaces_node_id;
      const _errs9 = errors;
      let valid3 = false;
      const _errs10 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err8 = {
            instancePath: instancePath + '/replaces_node_id',
            schemaPath: '#/properties/replaces_node_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/replaces_node_id',
          schemaPath: '#/properties/replaces_node_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid3 = valid3 || _valid0;
      const _errs12 = errors;
      if (data3 !== null) {
        const err10 = {
          instancePath: instancePath + '/replaces_node_id',
          schemaPath: '#/properties/replaces_node_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid0 = _errs12 === errors;
      valid3 = valid3 || _valid0;
      if (!valid3) {
        const err11 = {
          instancePath: instancePath + '/replaces_node_id',
          schemaPath: '#/properties/replaces_node_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err12 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err12];
    } else {
      vErrors.push(err12);
    }
    errors++;
  }
  validate141.errors = vErrors;
  return errors === 0;
}
validate141.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate140(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate140.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate141(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate141.errors : vErrors.concat(validate141.errors);
    errors = vErrors.length;
  }
  validate140.errors = vErrors;
  return errors === 0;
}
validate140.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__decommission_node_nodes__node_id__decommission_post = validate143;
const schema75 = { $ref: 'components.json#/$defs/NodeReasonBody' };
const schema76 = {
  properties: { reason_es: { type: 'string' } },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es'],
};

function validate144(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate144.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err2 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate144.errors = vErrors;
  return errors === 0;
}
validate144.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate143(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate143.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate144(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate144.errors : vErrors.concat(validate144.errors);
    errors = vErrors.length;
  }
  validate143.errors = vErrors;
  return errors === 0;
}
validate143.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__enroll_auth_second_factor_enroll_post = validate146;
const schema77 = { $ref: 'components.json#/$defs/EnrollRequest' };
const schema78 = {
  properties: {
    code: { anyOf: [{ type: 'string', maxLength: 64, minLength: 1 }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};

function validate147(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate147.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'code')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.code !== undefined) {
      let data0 = data.code;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 64) {
          const err1 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err2 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err4 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate147.errors = vErrors;
  return errors === 0;
}
validate147.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate146(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate146.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate147(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate147.errors : vErrors.concat(validate147.errors);
    errors = vErrors.length;
  }
  validate146.errors = vErrors;
  return errors === 0;
}
validate146.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__evaluate_plants__plant_id__admissions_post = validate149;
const schema79 = { $ref: 'components.json#/$defs/AdmissionBody' };
const schema80 = {
  properties: {
    family: { $ref: 'components.json#/$defs/PredicateFamily' },
    answers: { $ref: 'components.json#/$defs/AnswersBody' },
    justification_es: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['family', 'answers'],
};
const schema81 = {
  type: 'string',
  enum: ['coexistence', 'guard_bypass', 'dwell', 'startup_transition'],
};

function validate151(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate151.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'coexistence' ||
    data === 'guard_bypass' ||
    data === 'dwell' ||
    data === 'startup_transition'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema81.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate151.errors = vErrors;
  return errors === 0;
}
validate151.evaluated = { dynamicProps: false, dynamicItems: false };

const schema82 = {
  properties: {
    standard: { type: 'boolean' },
    remedy: { type: 'boolean' },
    subject: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['standard', 'remedy', 'subject'],
};

function validate153(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate153.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.standard === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'standard' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.remedy === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'remedy' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.subject === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'subject' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'standard' || key0 === 'remedy' || key0 === 'subject')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.standard !== undefined) {
      if (typeof data.standard !== 'boolean') {
        const err4 = {
          instancePath: instancePath + '/standard',
          schemaPath: '#/properties/standard/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.remedy !== undefined) {
      if (typeof data.remedy !== 'boolean') {
        const err5 = {
          instancePath: instancePath + '/remedy',
          schemaPath: '#/properties/remedy/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.subject !== undefined) {
      if (typeof data.subject !== 'boolean') {
        const err6 = {
          instancePath: instancePath + '/subject',
          schemaPath: '#/properties/subject/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate153.errors = vErrors;
  return errors === 0;
}
validate153.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate150(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate150.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.family === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'family' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.answers === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'answers' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'family' || key0 === 'answers' || key0 === 'justification_es')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.family !== undefined) {
      if (
        !validate151(data.family, {
          instancePath: instancePath + '/family',
          parentData: data,
          parentDataProperty: 'family',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate151.errors : vErrors.concat(validate151.errors);
        errors = vErrors.length;
      }
    }
    if (data.answers !== undefined) {
      if (
        !validate153(data.answers, {
          instancePath: instancePath + '/answers',
          parentData: data,
          parentDataProperty: 'answers',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate153.errors : vErrors.concat(validate153.errors);
        errors = vErrors.length;
      }
    }
    if (data.justification_es !== undefined) {
      let data2 = data.justification_es;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (typeof data2 !== 'string') {
        const err3 = {
          instancePath: instancePath + '/justification_es',
          schemaPath: '#/properties/justification_es/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data2 !== null) {
        const err4 = {
          instancePath: instancePath + '/justification_es',
          schemaPath: '#/properties/justification_es/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/justification_es',
          schemaPath: '#/properties/justification_es/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate150.errors = vErrors;
  return errors === 0;
}
validate150.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate149(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate149.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate150(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate150.errors : vErrors.concat(validate150.errors);
    errors = vErrors.length;
  }
  validate149.errors = vErrors;
  return errors === 0;
}
validate149.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__fleet_node_fleet_nodes__node_id__get = validate156;
const schema83 = {
  type: 'object',
  properties: {
    history_after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    history_limit: { type: 'integer', maximum: 100, minimum: 1 },
  },
  additionalProperties: false,
};

function validate156(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate156.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'history_after' || key0 === 'history_limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.history_after !== undefined) {
      let data0 = data.history_after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/history_after',
            schemaPath: '#/properties/history_after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/history_after',
          schemaPath: '#/properties/history_after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/history_after',
          schemaPath: '#/properties/history_after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/history_after',
          schemaPath: '#/properties/history_after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.history_limit !== undefined) {
      let data1 = data.history_limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/history_limit',
          schemaPath: '#/properties/history_limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 100 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/history_limit',
            schemaPath: '#/properties/history_limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 100 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/history_limit',
            schemaPath: '#/properties/history_limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate156.errors = vErrors;
  return errors === 0;
}
validate156.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__framing_recapture_zones__zone_id__framing_recaptures_post = validate157;
const schema84 = { $ref: 'components.json#/$defs/FramingRecaptureBody' };
const schema85 = {
  properties: {
    camera_id: { type: 'string', format: 'uuid' },
    captured_at: { type: 'string', format: 'date-time' },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['camera_id', 'captured_at', 'reason_es'],
};

function validate158(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate158.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.camera_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'camera_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.captured_at === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'captured_at' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'camera_id' || key0 === 'captured_at' || key0 === 'reason_es')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.camera_id !== undefined) {
      let data0 = data.camera_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err4 = {
            instancePath: instancePath + '/camera_id',
            schemaPath: '#/properties/camera_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/camera_id',
          schemaPath: '#/properties/camera_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.captured_at !== undefined) {
      let data1 = data.captured_at;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err6 = {
            instancePath: instancePath + '/captured_at',
            schemaPath: '#/properties/captured_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/captured_at',
          schemaPath: '#/properties/captured_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err8 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate158.errors = vErrors;
  return errors === 0;
}
validate158.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate157(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate157.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate158(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate158.errors : vErrors.concat(validate158.errors);
    errors = vErrors.length;
  }
  validate157.errors = vErrors;
  return errors === 0;
}
validate157.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__get_adoption_accreditation_adoption_get = validate160;
const schema86 = {
  type: 'object',
  properties: {
    from: { type: 'string', maxLength: 10 },
    to: { type: 'string', maxLength: 10 },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    zone_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  required: ['from', 'to'],
  additionalProperties: false,
};

function validate160(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate160.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.from === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'from' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.to === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'to' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'from' || key0 === 'to' || key0 === 'plant_id' || key0 === 'zone_id')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.from !== undefined) {
      let data0 = data.from;
      if (typeof data0 === 'string') {
        if (func1(data0) > 10) {
          const err3 = {
            instancePath: instancePath + '/from',
            schemaPath: '#/properties/from/maxLength',
            keyword: 'maxLength',
            params: { limit: 10 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.to !== undefined) {
      let data1 = data.to;
      if (typeof data1 === 'string') {
        if (func1(data1) > 10) {
          const err5 = {
            instancePath: instancePath + '/to',
            schemaPath: '#/properties/to/maxLength',
            keyword: 'maxLength',
            params: { limit: 10 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data2 = data.plant_id;
      const _errs7 = errors;
      let valid1 = false;
      const _errs8 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err7 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      const _errs10 = errors;
      if (data2 !== null) {
        const err9 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err10 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs7;
        if (vErrors !== null) {
          if (_errs7) {
            vErrors.length = _errs7;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.zone_id !== undefined) {
      let data3 = data.zone_id;
      const _errs13 = errors;
      let valid2 = false;
      const _errs14 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err11 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      const _errs16 = errors;
      if (data3 !== null) {
        const err13 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid1 = _errs16 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err14 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err15 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err15];
    } else {
      vErrors.push(err15);
    }
    errors++;
  }
  validate160.errors = vErrors;
  return errors === 0;
}
validate160.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__get_exposure_accreditation_exposure_get = validate161;
const schema87 = {
  type: 'object',
  properties: {
    zone_id: { type: 'string', format: 'uuid' },
    period: { type: 'string', pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
  },
  required: ['zone_id', 'period'],
  additionalProperties: false,
};
const pattern4 = new RegExp('^[0-9]{4}-(0[1-9]|1[0-2])$', 'u');

function validate161(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate161.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.zone_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.period === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'period' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'zone_id' || key0 === 'period')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data0 = data.zone_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err3 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.period !== undefined) {
      let data1 = data.period;
      if (typeof data1 === 'string') {
        if (!pattern4.test(data1)) {
          const err5 = {
            instancePath: instancePath + '/period',
            schemaPath: '#/properties/period/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/period',
          schemaPath: '#/properties/period/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate161.errors = vErrors;
  return errors === 0;
}
validate161.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__get_monthly_package_accreditation_monthly_packages_get = validate162;
const schema88 = {
  type: 'object',
  properties: {
    zone_id: { type: 'string', format: 'uuid' },
    period: { type: 'string', pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
  },
  required: ['zone_id', 'period'],
  additionalProperties: false,
};

function validate162(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate162.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.zone_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.period === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'period' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'zone_id' || key0 === 'period')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data0 = data.zone_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err3 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.period !== undefined) {
      let data1 = data.period;
      if (typeof data1 === 'string') {
        if (!pattern4.test(data1)) {
          const err5 = {
            instancePath: instancePath + '/period',
            schemaPath: '#/properties/period/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/period',
          schemaPath: '#/properties/period/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate162.errors = vErrors;
  return errors === 0;
}
validate162.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__grant_document_documents_post = validate163;
const schema89 = { $ref: 'components.json#/$defs/DocumentGrantBody' };
const schema90 = {
  properties: {
    plant_id: { type: 'string', format: 'uuid' },
    kind: { $ref: 'components.json#/$defs/DocumentKind' },
    content_type: { $ref: 'components.json#/$defs/DocumentContentType' },
    size_bytes: { type: 'integer', maximum: 20971520, minimum: 1 },
    sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['plant_id', 'kind', 'content_type', 'size_bytes', 'sha256'],
};
const schema91 = {
  type: 'string',
  enum: ['scope_record', 'use_agreement', 'plant_policy', 'blur_check_capture'],
};

function validate165(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate165.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'scope_record' ||
    data === 'use_agreement' ||
    data === 'plant_policy' ||
    data === 'blur_check_capture'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema91.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate165.errors = vErrors;
  return errors === 0;
}
validate165.evaluated = { dynamicProps: false, dynamicItems: false };

const schema92 = { type: 'string', enum: ['application/pdf', 'image/jpeg', 'image/png'] };

function validate167(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate167.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'application/pdf' || data === 'image/jpeg' || data === 'image/png')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema92.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate167.errors = vErrors;
  return errors === 0;
}
validate167.evaluated = { dynamicProps: false, dynamicItems: false };

function validate164(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate164.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.plant_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'plant_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.kind === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'kind' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.content_type === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'content_type' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.size_bytes === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'size_bytes' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.sha256 === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'sha256' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'plant_id' ||
        key0 === 'kind' ||
        key0 === 'content_type' ||
        key0 === 'size_bytes' ||
        key0 === 'sha256'
      )) {
        const err5 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data0 = data.plant_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err6 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.kind !== undefined) {
      if (
        !validate165(data.kind, {
          instancePath: instancePath + '/kind',
          parentData: data,
          parentDataProperty: 'kind',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate165.errors : vErrors.concat(validate165.errors);
        errors = vErrors.length;
      }
    }
    if (data.content_type !== undefined) {
      if (
        !validate167(data.content_type, {
          instancePath: instancePath + '/content_type',
          parentData: data,
          parentDataProperty: 'content_type',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate167.errors : vErrors.concat(validate167.errors);
        errors = vErrors.length;
      }
    }
    if (data.size_bytes !== undefined) {
      let data3 = data.size_bytes;
      if (!(typeof data3 == 'number' && !(data3 % 1) && !isNaN(data3) && isFinite(data3))) {
        const err8 = {
          instancePath: instancePath + '/size_bytes',
          schemaPath: '#/properties/size_bytes/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      if (typeof data3 == 'number' && isFinite(data3)) {
        if (data3 > 20971520 || isNaN(data3)) {
          const err9 = {
            instancePath: instancePath + '/size_bytes',
            schemaPath: '#/properties/size_bytes/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 20971520 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
        if (data3 < 1 || isNaN(data3)) {
          const err10 = {
            instancePath: instancePath + '/size_bytes',
            schemaPath: '#/properties/size_bytes/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      }
    }
    if (data.sha256 !== undefined) {
      let data4 = data.sha256;
      if (typeof data4 === 'string') {
        if (!pattern3.test(data4)) {
          const err11 = {
            instancePath: instancePath + '/sha256',
            schemaPath: '#/properties/sha256/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9a-f]{64}$' },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/sha256',
          schemaPath: '#/properties/sha256/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
    }
  } else {
    const err13 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err13];
    } else {
      vErrors.push(err13);
    }
    errors++;
  }
  validate164.errors = vErrors;
  return errors === 0;
}
validate164.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate163(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate163.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate164(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate164.errors : vErrors.concat(validate164.errors);
    errors = vErrors.length;
  }
  validate163.errors = vErrors;
  return errors === 0;
}
validate163.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__grant_provider_concessions_post = validate170;
const schema93 = { $ref: 'components.json#/$defs/GrantBody' };
const schema94 = {
  properties: {
    client_organization_id: { type: 'string', format: 'uuid' },
    scope_level: { type: 'string', enum: ['organization', 'plant'] },
    scope_id: { type: 'string', format: 'uuid' },
    reason: { type: 'string', maxLength: 500, minLength: 10 },
    duration_hours: { anyOf: [{ type: 'integer', maximum: 2160, minimum: 1 }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['client_organization_id', 'scope_level', 'scope_id', 'reason'],
};

function validate171(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate171.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.client_organization_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'client_organization_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.scope_level === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_level' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.scope_id === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_id' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.reason === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'client_organization_id' ||
        key0 === 'scope_level' ||
        key0 === 'scope_id' ||
        key0 === 'reason' ||
        key0 === 'duration_hours'
      )) {
        const err4 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.client_organization_id !== undefined) {
      let data0 = data.client_organization_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err5 = {
            instancePath: instancePath + '/client_organization_id',
            schemaPath: '#/properties/client_organization_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/client_organization_id',
          schemaPath: '#/properties/client_organization_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.scope_level !== undefined) {
      let data1 = data.scope_level;
      if (typeof data1 !== 'string') {
        const err7 = {
          instancePath: instancePath + '/scope_level',
          schemaPath: '#/properties/scope_level/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      if (!(data1 === 'organization' || data1 === 'plant')) {
        const err8 = {
          instancePath: instancePath + '/scope_level',
          schemaPath: '#/properties/scope_level/enum',
          keyword: 'enum',
          params: { allowedValues: schema94.properties.scope_level.enum },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.scope_id !== undefined) {
      let data2 = data.scope_id;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err9 = {
            instancePath: instancePath + '/scope_id',
            schemaPath: '#/properties/scope_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
      } else {
        const err10 = {
          instancePath: instancePath + '/scope_id',
          schemaPath: '#/properties/scope_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
    if (data.reason !== undefined) {
      let data3 = data.reason;
      if (typeof data3 === 'string') {
        if (func1(data3) > 500) {
          const err11 = {
            instancePath: instancePath + '/reason',
            schemaPath: '#/properties/reason/maxLength',
            keyword: 'maxLength',
            params: { limit: 500 },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
        if (func1(data3) < 10) {
          const err12 = {
            instancePath: instancePath + '/reason',
            schemaPath: '#/properties/reason/minLength',
            keyword: 'minLength',
            params: { limit: 10 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
      } else {
        const err13 = {
          instancePath: instancePath + '/reason',
          schemaPath: '#/properties/reason/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
    }
    if (data.duration_hours !== undefined) {
      let data4 = data.duration_hours;
      const _errs11 = errors;
      let valid1 = false;
      const _errs12 = errors;
      if (!(typeof data4 == 'number' && !(data4 % 1) && !isNaN(data4) && isFinite(data4))) {
        const err14 = {
          instancePath: instancePath + '/duration_hours',
          schemaPath: '#/properties/duration_hours/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      if (typeof data4 == 'number' && isFinite(data4)) {
        if (data4 > 2160 || isNaN(data4)) {
          const err15 = {
            instancePath: instancePath + '/duration_hours',
            schemaPath: '#/properties/duration_hours/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 2160 },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
        if (data4 < 1 || isNaN(data4)) {
          const err16 = {
            instancePath: instancePath + '/duration_hours',
            schemaPath: '#/properties/duration_hours/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err16];
          } else {
            vErrors.push(err16);
          }
          errors++;
        }
      }
      var _valid0 = _errs12 === errors;
      valid1 = valid1 || _valid0;
      const _errs14 = errors;
      if (data4 !== null) {
        const err17 = {
          instancePath: instancePath + '/duration_hours',
          schemaPath: '#/properties/duration_hours/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      var _valid0 = _errs14 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err18 = {
          instancePath: instancePath + '/duration_hours',
          schemaPath: '#/properties/duration_hours/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err19 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err19];
    } else {
      vErrors.push(err19);
    }
    errors++;
  }
  validate171.errors = vErrors;
  return errors === 0;
}
validate171.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate170(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate170.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate171(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate171.errors : vErrors.concat(validate171.errors);
    errors = vErrors.length;
  }
  validate170.errors = vErrors;
  return errors === 0;
}
validate170.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__invite_users_post = validate173;
const schema95 = { $ref: 'components.json#/$defs/InviteBody' };
const schema96 = {
  properties: {
    email: { type: 'string', maxLength: 254, minLength: 3 },
    display_name: { type: 'string', maxLength: 120, minLength: 1 },
    professional_license: {
      anyOf: [{ type: 'string', maxLength: 64, minLength: 1 }, { type: 'null' }],
    },
    assignments: {
      items: { $ref: 'components.json#/$defs/AssignmentBody' },
      type: 'array',
      maxItems: 32,
      minItems: 1,
    },
    disclose_link: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['email', 'display_name', 'assignments'],
};

function validate175(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate175.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.role === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'role' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.scope_level === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_level' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.scope_id === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_id' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'role' || key0 === 'scope_level' || key0 === 'scope_id')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.role !== undefined) {
      if (
        !validate71(data.role, {
          instancePath: instancePath + '/role',
          parentData: data,
          parentDataProperty: 'role',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate71.errors : vErrors.concat(validate71.errors);
        errors = vErrors.length;
      }
    }
    if (data.scope_level !== undefined) {
      if (
        !validate73(data.scope_level, {
          instancePath: instancePath + '/scope_level',
          parentData: data,
          parentDataProperty: 'scope_level',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate73.errors : vErrors.concat(validate73.errors);
        errors = vErrors.length;
      }
    }
    if (data.scope_id !== undefined) {
      let data2 = data.scope_id;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err4 = {
            instancePath: instancePath + '/scope_id',
            schemaPath: '#/properties/scope_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/scope_id',
          schemaPath: '#/properties/scope_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate175.errors = vErrors;
  return errors === 0;
}
validate175.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate174(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate174.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.email === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'email' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.display_name === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'display_name' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.assignments === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'assignments' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'email' ||
        key0 === 'display_name' ||
        key0 === 'professional_license' ||
        key0 === 'assignments' ||
        key0 === 'disclose_link'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.email !== undefined) {
      let data0 = data.email;
      if (typeof data0 === 'string') {
        if (func1(data0) > 254) {
          const err4 = {
            instancePath: instancePath + '/email',
            schemaPath: '#/properties/email/maxLength',
            keyword: 'maxLength',
            params: { limit: 254 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        if (func1(data0) < 3) {
          const err5 = {
            instancePath: instancePath + '/email',
            schemaPath: '#/properties/email/minLength',
            keyword: 'minLength',
            params: { limit: 3 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/email',
          schemaPath: '#/properties/email/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.display_name !== undefined) {
      let data1 = data.display_name;
      if (typeof data1 === 'string') {
        if (func1(data1) > 120) {
          const err7 = {
            instancePath: instancePath + '/display_name',
            schemaPath: '#/properties/display_name/maxLength',
            keyword: 'maxLength',
            params: { limit: 120 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err8 = {
            instancePath: instancePath + '/display_name',
            schemaPath: '#/properties/display_name/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/display_name',
          schemaPath: '#/properties/display_name/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.professional_license !== undefined) {
      let data2 = data.professional_license;
      const _errs7 = errors;
      let valid1 = false;
      const _errs8 = errors;
      if (typeof data2 === 'string') {
        if (func1(data2) > 64) {
          const err10 = {
            instancePath: instancePath + '/professional_license',
            schemaPath: '#/properties/professional_license/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
        if (func1(data2) < 1) {
          const err11 = {
            instancePath: instancePath + '/professional_license',
            schemaPath: '#/properties/professional_license/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      const _errs10 = errors;
      if (data2 !== null) {
        const err13 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err14 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      } else {
        errors = _errs7;
        if (vErrors !== null) {
          if (_errs7) {
            vErrors.length = _errs7;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.assignments !== undefined) {
      let data3 = data.assignments;
      if (Array.isArray(data3)) {
        if (data3.length > 32) {
          const err15 = {
            instancePath: instancePath + '/assignments',
            schemaPath: '#/properties/assignments/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
        if (data3.length < 1) {
          const err16 = {
            instancePath: instancePath + '/assignments',
            schemaPath: '#/properties/assignments/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err16];
          } else {
            vErrors.push(err16);
          }
          errors++;
        }
        const len0 = data3.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate175(data3[i0], {
              instancePath: instancePath + '/assignments/' + i0,
              parentData: data3,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate175.errors : vErrors.concat(validate175.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err17 = {
          instancePath: instancePath + '/assignments',
          schemaPath: '#/properties/assignments/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
    }
    if (data.disclose_link !== undefined) {
      if (typeof data.disclose_link !== 'boolean') {
        const err18 = {
          instancePath: instancePath + '/disclose_link',
          schemaPath: '#/properties/disclose_link/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      }
    }
  } else {
    const err19 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err19];
    } else {
      vErrors.push(err19);
    }
    errors++;
  }
  validate174.errors = vErrors;
  return errors === 0;
}
validate174.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate173(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate173.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate174(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate174.errors : vErrors.concat(validate174.errors);
    errors = vErrors.length;
  }
  validate173.errors = vErrors;
  return errors === 0;
}
validate173.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_admissions_plants__plant_id__admissions_get = validate180;
const schema98 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate180(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate180.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'after' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate180.errors = vErrors;
  return errors === 0;
}
validate180.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_audit_entries_audit_entries_get = validate181;
const schema99 = {
  type: 'object',
  properties: {
    operation: {
      anyOf: [
        {
          type: 'array',
          items: { type: 'string', pattern: '^[a-z][a-z0-9_]{0,63}$' },
          maxItems: 32,
        },
        { type: 'null' },
      ],
    },
    actor_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    zone_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    occurred_from: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    occurred_before: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    page_size: { type: 'integer', maximum: 200, minimum: 1 },
    after_occurred_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    after_entry_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
};
const pattern7 = new RegExp('^[a-z][a-z0-9_]{0,63}$', 'u');

function validate181(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate181.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!func27.call(schema99.properties, key0)) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.operation !== undefined) {
      let data0 = data.operation;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (Array.isArray(data0)) {
        if (data0.length > 32) {
          const err1 = {
            instancePath: instancePath + '/operation',
            schemaPath: '#/properties/operation/anyOf/0/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data1 = data0[i0];
          if (typeof data1 === 'string') {
            if (!pattern7.test(data1)) {
              const err2 = {
                instancePath: instancePath + '/operation/' + i0,
                schemaPath: '#/properties/operation/anyOf/0/items/pattern',
                keyword: 'pattern',
                params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
              };
              if (vErrors === null) {
                vErrors = [err2];
              } else {
                vErrors.push(err2);
              }
              errors++;
            }
          } else {
            const err3 = {
              instancePath: instancePath + '/operation/' + i0,
              schemaPath: '#/properties/operation/anyOf/0/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err3];
            } else {
              vErrors.push(err3);
            }
            errors++;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/operation',
          schemaPath: '#/properties/operation/anyOf/0/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data0 !== null) {
        const err5 = {
          instancePath: instancePath + '/operation',
          schemaPath: '#/properties/operation/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err6 = {
          instancePath: instancePath + '/operation',
          schemaPath: '#/properties/operation/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.actor_id !== undefined) {
      let data2 = data.actor_id;
      const _errs11 = errors;
      let valid4 = false;
      const _errs12 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err7 = {
            instancePath: instancePath + '/actor_id',
            schemaPath: '#/properties/actor_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/actor_id',
          schemaPath: '#/properties/actor_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid4 = valid4 || _valid1;
      const _errs14 = errors;
      if (data2 !== null) {
        const err9 = {
          instancePath: instancePath + '/actor_id',
          schemaPath: '#/properties/actor_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err10 = {
          instancePath: instancePath + '/actor_id',
          schemaPath: '#/properties/actor_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.plant_id !== undefined) {
      let data3 = data.plant_id;
      const _errs17 = errors;
      let valid5 = false;
      const _errs18 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err11 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid5 = valid5 || _valid2;
      const _errs20 = errors;
      if (data3 !== null) {
        const err13 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid2 = _errs20 === errors;
      valid5 = valid5 || _valid2;
      if (!valid5) {
        const err14 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      } else {
        errors = _errs17;
        if (vErrors !== null) {
          if (_errs17) {
            vErrors.length = _errs17;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.zone_id !== undefined) {
      let data4 = data.zone_id;
      const _errs23 = errors;
      let valid6 = false;
      const _errs24 = errors;
      if (typeof data4 === 'string') {
        if (!formats0.test(data4)) {
          const err15 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
      } else {
        const err16 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid6 = valid6 || _valid3;
      const _errs26 = errors;
      if (data4 !== null) {
        const err17 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      var _valid3 = _errs26 === errors;
      valid6 = valid6 || _valid3;
      if (!valid6) {
        const err18 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      } else {
        errors = _errs23;
        if (vErrors !== null) {
          if (_errs23) {
            vErrors.length = _errs23;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.occurred_from !== undefined) {
      let data5 = data.occurred_from;
      const _errs29 = errors;
      let valid7 = false;
      const _errs30 = errors;
      if (typeof data5 !== 'string') {
        const err19 = {
          instancePath: instancePath + '/occurred_from',
          schemaPath: '#/properties/occurred_from/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid7 = valid7 || _valid4;
      const _errs32 = errors;
      if (data5 !== null) {
        const err20 = {
          instancePath: instancePath + '/occurred_from',
          schemaPath: '#/properties/occurred_from/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid4 = _errs32 === errors;
      valid7 = valid7 || _valid4;
      if (!valid7) {
        const err21 = {
          instancePath: instancePath + '/occurred_from',
          schemaPath: '#/properties/occurred_from/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      } else {
        errors = _errs29;
        if (vErrors !== null) {
          if (_errs29) {
            vErrors.length = _errs29;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.occurred_before !== undefined) {
      let data6 = data.occurred_before;
      const _errs35 = errors;
      let valid8 = false;
      const _errs36 = errors;
      if (typeof data6 !== 'string') {
        const err22 = {
          instancePath: instancePath + '/occurred_before',
          schemaPath: '#/properties/occurred_before/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err22];
        } else {
          vErrors.push(err22);
        }
        errors++;
      }
      var _valid5 = _errs36 === errors;
      valid8 = valid8 || _valid5;
      const _errs38 = errors;
      if (data6 !== null) {
        const err23 = {
          instancePath: instancePath + '/occurred_before',
          schemaPath: '#/properties/occurred_before/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err23];
        } else {
          vErrors.push(err23);
        }
        errors++;
      }
      var _valid5 = _errs38 === errors;
      valid8 = valid8 || _valid5;
      if (!valid8) {
        const err24 = {
          instancePath: instancePath + '/occurred_before',
          schemaPath: '#/properties/occurred_before/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err24];
        } else {
          vErrors.push(err24);
        }
        errors++;
      } else {
        errors = _errs35;
        if (vErrors !== null) {
          if (_errs35) {
            vErrors.length = _errs35;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.page_size !== undefined) {
      let data7 = data.page_size;
      if (!(typeof data7 == 'number' && !(data7 % 1) && !isNaN(data7) && isFinite(data7))) {
        const err25 = {
          instancePath: instancePath + '/page_size',
          schemaPath: '#/properties/page_size/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err25];
        } else {
          vErrors.push(err25);
        }
        errors++;
      }
      if (typeof data7 == 'number' && isFinite(data7)) {
        if (data7 > 200 || isNaN(data7)) {
          const err26 = {
            instancePath: instancePath + '/page_size',
            schemaPath: '#/properties/page_size/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err26];
          } else {
            vErrors.push(err26);
          }
          errors++;
        }
        if (data7 < 1 || isNaN(data7)) {
          const err27 = {
            instancePath: instancePath + '/page_size',
            schemaPath: '#/properties/page_size/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err27];
          } else {
            vErrors.push(err27);
          }
          errors++;
        }
      }
    }
    if (data.after_occurred_at !== undefined) {
      let data8 = data.after_occurred_at;
      const _errs43 = errors;
      let valid9 = false;
      const _errs44 = errors;
      if (typeof data8 !== 'string') {
        const err28 = {
          instancePath: instancePath + '/after_occurred_at',
          schemaPath: '#/properties/after_occurred_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err28];
        } else {
          vErrors.push(err28);
        }
        errors++;
      }
      var _valid6 = _errs44 === errors;
      valid9 = valid9 || _valid6;
      const _errs46 = errors;
      if (data8 !== null) {
        const err29 = {
          instancePath: instancePath + '/after_occurred_at',
          schemaPath: '#/properties/after_occurred_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err29];
        } else {
          vErrors.push(err29);
        }
        errors++;
      }
      var _valid6 = _errs46 === errors;
      valid9 = valid9 || _valid6;
      if (!valid9) {
        const err30 = {
          instancePath: instancePath + '/after_occurred_at',
          schemaPath: '#/properties/after_occurred_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err30];
        } else {
          vErrors.push(err30);
        }
        errors++;
      } else {
        errors = _errs43;
        if (vErrors !== null) {
          if (_errs43) {
            vErrors.length = _errs43;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.after_entry_id !== undefined) {
      let data9 = data.after_entry_id;
      const _errs49 = errors;
      let valid10 = false;
      const _errs50 = errors;
      if (typeof data9 === 'string') {
        if (!formats0.test(data9)) {
          const err31 = {
            instancePath: instancePath + '/after_entry_id',
            schemaPath: '#/properties/after_entry_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err31];
          } else {
            vErrors.push(err31);
          }
          errors++;
        }
      } else {
        const err32 = {
          instancePath: instancePath + '/after_entry_id',
          schemaPath: '#/properties/after_entry_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err32];
        } else {
          vErrors.push(err32);
        }
        errors++;
      }
      var _valid7 = _errs50 === errors;
      valid10 = valid10 || _valid7;
      const _errs52 = errors;
      if (data9 !== null) {
        const err33 = {
          instancePath: instancePath + '/after_entry_id',
          schemaPath: '#/properties/after_entry_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err33];
        } else {
          vErrors.push(err33);
        }
        errors++;
      }
      var _valid7 = _errs52 === errors;
      valid10 = valid10 || _valid7;
      if (!valid10) {
        const err34 = {
          instancePath: instancePath + '/after_entry_id',
          schemaPath: '#/properties/after_entry_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err34];
        } else {
          vErrors.push(err34);
        }
        errors++;
      } else {
        errors = _errs49;
        if (vErrors !== null) {
          if (_errs49) {
            vErrors.length = _errs49;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err35 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err35];
    } else {
      vErrors.push(err35);
    }
    errors++;
  }
  validate181.errors = vErrors;
  return errors === 0;
}
validate181.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_commitments_accreditation_commitments_get = validate182;
const schema100 = {
  type: 'object',
  properties: {
    zone_id: { type: 'string', format: 'uuid' },
    period: { type: 'string', pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
  },
  required: ['zone_id', 'period'],
  additionalProperties: false,
};

function validate182(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate182.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.zone_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.period === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'period' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'zone_id' || key0 === 'period')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data0 = data.zone_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err3 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.period !== undefined) {
      let data1 = data.period;
      if (typeof data1 === 'string') {
        if (!pattern4.test(data1)) {
          const err5 = {
            instancePath: instancePath + '/period',
            schemaPath: '#/properties/period/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/period',
          schemaPath: '#/properties/period/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate182.errors = vErrors;
  return errors === 0;
}
validate182.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_concessions_concessions_get = validate183;
const schema101 = {
  type: 'object',
  properties: { plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] } },
  additionalProperties: false,
};

function validate183(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate183.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'plant_id')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data0 = data.plant_id;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err1 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate183.errors = vErrors;
  return errors === 0;
}
validate183.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_enrollment_attempts_nodes__node_id__enrollment_attempts_get = validate184;
const schema102 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate184(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate184.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'after' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate184.errors = vErrors;
  return errors === 0;
}
validate184.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_findings_loop_findings_get = validate185;
const schema103 = {
  type: 'object',
  properties: {
    zone_id: {
      anyOf: [{ type: 'array', items: { type: 'string', format: 'uuid' } }, { type: 'null' }],
    },
    family: { anyOf: [{ $ref: 'components.json#/$defs/PredicateFamily' }, { type: 'null' }] },
    tier: { anyOf: [{ $ref: 'components.json#/$defs/EvidenceTier' }, { type: 'null' }] },
    lifecycle_state: { anyOf: [{ type: 'array', items: { type: 'string' } }, { type: 'null' }] },
    from: { anyOf: [{ type: 'string', maxLength: 40 }, { type: 'null' }] },
    to: { anyOf: [{ type: 'string', maxLength: 40 }, { type: 'null' }] },
    order: { $ref: 'components.json#/$defs/InboxOrder' },
    cursor: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate186(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate186.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'coexistence' ||
    data === 'guard_bypass' ||
    data === 'dwell' ||
    data === 'startup_transition'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema81.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate186.errors = vErrors;
  return errors === 0;
}
validate186.evaluated = { dynamicProps: false, dynamicItems: false };

const schema105 = { type: 'string', enum: ['tier_1', 'tier_2'] };

function validate188(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate188.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'tier_1' || data === 'tier_2')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema105.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate188.errors = vErrors;
  return errors === 0;
}
validate188.evaluated = { dynamicProps: false, dynamicItems: false };

const schema106 = { type: 'string', enum: ['newest_first', 'oldest_first'] };

function validate190(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate190.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'newest_first' || data === 'oldest_first')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema106.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate190.errors = vErrors;
  return errors === 0;
}
validate190.evaluated = { dynamicProps: false, dynamicItems: false };

function validate185(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate185.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!func27.call(schema103.properties, key0)) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data0 = data.zone_id;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (Array.isArray(data0)) {
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data1 = data0[i0];
          if (typeof data1 === 'string') {
            if (!formats0.test(data1)) {
              const err1 = {
                instancePath: instancePath + '/zone_id/' + i0,
                schemaPath: '#/properties/zone_id/anyOf/0/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err1];
              } else {
                vErrors.push(err1);
              }
              errors++;
            }
          } else {
            const err2 = {
              instancePath: instancePath + '/zone_id/' + i0,
              schemaPath: '#/properties/zone_id/anyOf/0/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err2];
            } else {
              vErrors.push(err2);
            }
            errors++;
          }
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data0 !== null) {
        const err4 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.family !== undefined) {
      let data2 = data.family;
      const _errs11 = errors;
      let valid4 = false;
      const _errs12 = errors;
      if (
        !validate186(data2, {
          instancePath: instancePath + '/family',
          parentData: data,
          parentDataProperty: 'family',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate186.errors : vErrors.concat(validate186.errors);
        errors = vErrors.length;
      }
      var _valid1 = _errs12 === errors;
      valid4 = valid4 || _valid1;
      const _errs13 = errors;
      if (data2 !== null) {
        const err6 = {
          instancePath: instancePath + '/family',
          schemaPath: '#/properties/family/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid1 = _errs13 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err7 = {
          instancePath: instancePath + '/family',
          schemaPath: '#/properties/family/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.tier !== undefined) {
      let data3 = data.tier;
      const _errs16 = errors;
      let valid5 = false;
      const _errs17 = errors;
      if (
        !validate188(data3, {
          instancePath: instancePath + '/tier',
          parentData: data,
          parentDataProperty: 'tier',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate188.errors : vErrors.concat(validate188.errors);
        errors = vErrors.length;
      }
      var _valid2 = _errs17 === errors;
      valid5 = valid5 || _valid2;
      const _errs18 = errors;
      if (data3 !== null) {
        const err8 = {
          instancePath: instancePath + '/tier',
          schemaPath: '#/properties/tier/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid5 = valid5 || _valid2;
      if (!valid5) {
        const err9 = {
          instancePath: instancePath + '/tier',
          schemaPath: '#/properties/tier/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs16;
        if (vErrors !== null) {
          if (_errs16) {
            vErrors.length = _errs16;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.lifecycle_state !== undefined) {
      let data4 = data.lifecycle_state;
      const _errs21 = errors;
      let valid6 = false;
      const _errs22 = errors;
      if (Array.isArray(data4)) {
        const len1 = data4.length;
        for (let i1 = 0; i1 < len1; i1++) {
          if (typeof data4[i1] !== 'string') {
            const err10 = {
              instancePath: instancePath + '/lifecycle_state/' + i1,
              schemaPath: '#/properties/lifecycle_state/anyOf/0/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err10];
            } else {
              vErrors.push(err10);
            }
            errors++;
          }
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/lifecycle_state',
          schemaPath: '#/properties/lifecycle_state/anyOf/0/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid3 = _errs22 === errors;
      valid6 = valid6 || _valid3;
      const _errs26 = errors;
      if (data4 !== null) {
        const err12 = {
          instancePath: instancePath + '/lifecycle_state',
          schemaPath: '#/properties/lifecycle_state/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid3 = _errs26 === errors;
      valid6 = valid6 || _valid3;
      if (!valid6) {
        const err13 = {
          instancePath: instancePath + '/lifecycle_state',
          schemaPath: '#/properties/lifecycle_state/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.from !== undefined) {
      let data6 = data.from;
      const _errs29 = errors;
      let valid9 = false;
      const _errs30 = errors;
      if (typeof data6 === 'string') {
        if (func1(data6) > 40) {
          const err14 = {
            instancePath: instancePath + '/from',
            schemaPath: '#/properties/from/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 40 },
          };
          if (vErrors === null) {
            vErrors = [err14];
          } else {
            vErrors.push(err14);
          }
          errors++;
        }
      } else {
        const err15 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid9 = valid9 || _valid4;
      const _errs32 = errors;
      if (data6 !== null) {
        const err16 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid4 = _errs32 === errors;
      valid9 = valid9 || _valid4;
      if (!valid9) {
        const err17 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      } else {
        errors = _errs29;
        if (vErrors !== null) {
          if (_errs29) {
            vErrors.length = _errs29;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.to !== undefined) {
      let data7 = data.to;
      const _errs35 = errors;
      let valid10 = false;
      const _errs36 = errors;
      if (typeof data7 === 'string') {
        if (func1(data7) > 40) {
          const err18 = {
            instancePath: instancePath + '/to',
            schemaPath: '#/properties/to/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 40 },
          };
          if (vErrors === null) {
            vErrors = [err18];
          } else {
            vErrors.push(err18);
          }
          errors++;
        }
      } else {
        const err19 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      }
      var _valid5 = _errs36 === errors;
      valid10 = valid10 || _valid5;
      const _errs38 = errors;
      if (data7 !== null) {
        const err20 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid5 = _errs38 === errors;
      valid10 = valid10 || _valid5;
      if (!valid10) {
        const err21 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      } else {
        errors = _errs35;
        if (vErrors !== null) {
          if (_errs35) {
            vErrors.length = _errs35;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.order !== undefined) {
      if (
        !validate190(data.order, {
          instancePath: instancePath + '/order',
          parentData: data,
          parentDataProperty: 'order',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate190.errors : vErrors.concat(validate190.errors);
        errors = vErrors.length;
      }
    }
    if (data.cursor !== undefined) {
      let data9 = data.cursor;
      const _errs42 = errors;
      let valid11 = false;
      const _errs43 = errors;
      if (typeof data9 === 'string') {
        if (func1(data9) > 128) {
          const err22 = {
            instancePath: instancePath + '/cursor',
            schemaPath: '#/properties/cursor/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err22];
          } else {
            vErrors.push(err22);
          }
          errors++;
        }
      } else {
        const err23 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err23];
        } else {
          vErrors.push(err23);
        }
        errors++;
      }
      var _valid6 = _errs43 === errors;
      valid11 = valid11 || _valid6;
      const _errs45 = errors;
      if (data9 !== null) {
        const err24 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err24];
        } else {
          vErrors.push(err24);
        }
        errors++;
      }
      var _valid6 = _errs45 === errors;
      valid11 = valid11 || _valid6;
      if (!valid11) {
        const err25 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err25];
        } else {
          vErrors.push(err25);
        }
        errors++;
      } else {
        errors = _errs42;
        if (vErrors !== null) {
          if (_errs42) {
            vErrors.length = _errs42;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data10 = data.limit;
      if (!(typeof data10 == 'number' && !(data10 % 1) && !isNaN(data10) && isFinite(data10))) {
        const err26 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err26];
        } else {
          vErrors.push(err26);
        }
        errors++;
      }
      if (typeof data10 == 'number' && isFinite(data10)) {
        if (data10 > 200 || isNaN(data10)) {
          const err27 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err27];
          } else {
            vErrors.push(err27);
          }
          errors++;
        }
        if (data10 < 1 || isNaN(data10)) {
          const err28 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err28];
          } else {
            vErrors.push(err28);
          }
          errors++;
        }
      }
    }
  } else {
    const err29 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err29];
    } else {
      vErrors.push(err29);
    }
    errors++;
  }
  validate185.errors = vErrors;
  return errors === 0;
}
validate185.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_fleet_alarms_fleet_alarms_get = validate192;
const schema107 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 100, minimum: 1 },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    node_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    alarm_kind: { anyOf: [{ $ref: 'components.json#/$defs/FleetAlarmKind' }, { type: 'null' }] },
    status: { anyOf: [{ $ref: 'components.json#/$defs/AlarmStatus' }, { type: 'null' }] },
  },
  additionalProperties: false,
};
const schema108 = {
  type: 'string',
  enum: [
    'node_mute',
    'queue_over_threshold',
    'clock_drift',
    'version_retiring',
    'simulated_adapter_in_productive',
    'certificate_expiring',
    'camera_below_min_fps',
    'orphan_clips_growing',
  ],
};

function validate193(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate193.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'node_mute' ||
    data === 'queue_over_threshold' ||
    data === 'clock_drift' ||
    data === 'version_retiring' ||
    data === 'simulated_adapter_in_productive' ||
    data === 'certificate_expiring' ||
    data === 'camera_below_min_fps' ||
    data === 'orphan_clips_growing'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema108.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate193.errors = vErrors;
  return errors === 0;
}
validate193.evaluated = { dynamicProps: false, dynamicItems: false };

const schema109 = { type: 'string', enum: ['active', 'cleared'] };

function validate195(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate195.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'active' || data === 'cleared')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema109.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate195.errors = vErrors;
  return errors === 0;
}
validate195.evaluated = { dynamicProps: false, dynamicItems: false };

function validate192(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate192.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'after' ||
        key0 === 'limit' ||
        key0 === 'plant_id' ||
        key0 === 'node_id' ||
        key0 === 'alarm_kind' ||
        key0 === 'status'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 100 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 100 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
    if (data.plant_id !== undefined) {
      let data2 = data.plant_id;
      const _errs11 = errors;
      let valid2 = false;
      const _errs12 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err8 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      const _errs14 = errors;
      if (data2 !== null) {
        const err10 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err11 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.node_id !== undefined) {
      let data3 = data.node_id;
      const _errs17 = errors;
      let valid3 = false;
      const _errs18 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err12 = {
            instancePath: instancePath + '/node_id',
            schemaPath: '#/properties/node_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
      } else {
        const err13 = {
          instancePath: instancePath + '/node_id',
          schemaPath: '#/properties/node_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      const _errs20 = errors;
      if (data3 !== null) {
        const err14 = {
          instancePath: instancePath + '/node_id',
          schemaPath: '#/properties/node_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid2 = _errs20 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err15 = {
          instancePath: instancePath + '/node_id',
          schemaPath: '#/properties/node_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      } else {
        errors = _errs17;
        if (vErrors !== null) {
          if (_errs17) {
            vErrors.length = _errs17;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.alarm_kind !== undefined) {
      let data4 = data.alarm_kind;
      const _errs23 = errors;
      let valid4 = false;
      const _errs24 = errors;
      if (
        !validate193(data4, {
          instancePath: instancePath + '/alarm_kind',
          parentData: data,
          parentDataProperty: 'alarm_kind',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate193.errors : vErrors.concat(validate193.errors);
        errors = vErrors.length;
      }
      var _valid3 = _errs24 === errors;
      valid4 = valid4 || _valid3;
      const _errs25 = errors;
      if (data4 !== null) {
        const err16 = {
          instancePath: instancePath + '/alarm_kind',
          schemaPath: '#/properties/alarm_kind/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid3 = _errs25 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err17 = {
          instancePath: instancePath + '/alarm_kind',
          schemaPath: '#/properties/alarm_kind/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      } else {
        errors = _errs23;
        if (vErrors !== null) {
          if (_errs23) {
            vErrors.length = _errs23;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.status !== undefined) {
      let data5 = data.status;
      const _errs28 = errors;
      let valid5 = false;
      const _errs29 = errors;
      if (
        !validate195(data5, {
          instancePath: instancePath + '/status',
          parentData: data,
          parentDataProperty: 'status',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate195.errors : vErrors.concat(validate195.errors);
        errors = vErrors.length;
      }
      var _valid4 = _errs29 === errors;
      valid5 = valid5 || _valid4;
      const _errs30 = errors;
      if (data5 !== null) {
        const err18 = {
          instancePath: instancePath + '/status',
          schemaPath: '#/properties/status/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid5 = valid5 || _valid4;
      if (!valid5) {
        const err19 = {
          instancePath: instancePath + '/status',
          schemaPath: '#/properties/status/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      } else {
        errors = _errs28;
        if (vErrors !== null) {
          if (_errs28) {
            vErrors.length = _errs28;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err20 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err20];
    } else {
      vErrors.push(err20);
    }
    errors++;
  }
  validate192.errors = vErrors;
  return errors === 0;
}
validate192.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_fleet_nodes_fleet_nodes_get = validate197;
const schema110 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 100, minimum: 1 },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    communication_state: {
      anyOf: [{ $ref: 'components.json#/$defs/CommunicationState' }, { type: 'null' }],
    },
    warning: { anyOf: [{ $ref: 'components.json#/$defs/FleetAlarmKind' }, { type: 'null' }] },
  },
  additionalProperties: false,
};
const schema111 = { type: 'string', enum: ['unknown', 'reachable', 'mute'] };

function validate198(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate198.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'unknown' || data === 'reachable' || data === 'mute')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema111.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate198.errors = vErrors;
  return errors === 0;
}
validate198.evaluated = { dynamicProps: false, dynamicItems: false };

function validate200(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate200.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'node_mute' ||
    data === 'queue_over_threshold' ||
    data === 'clock_drift' ||
    data === 'version_retiring' ||
    data === 'simulated_adapter_in_productive' ||
    data === 'certificate_expiring' ||
    data === 'camera_below_min_fps' ||
    data === 'orphan_clips_growing'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema108.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate200.errors = vErrors;
  return errors === 0;
}
validate200.evaluated = { dynamicProps: false, dynamicItems: false };

function validate197(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate197.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'after' ||
        key0 === 'limit' ||
        key0 === 'plant_id' ||
        key0 === 'communication_state' ||
        key0 === 'warning'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 100 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 100 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
    if (data.plant_id !== undefined) {
      let data2 = data.plant_id;
      const _errs11 = errors;
      let valid2 = false;
      const _errs12 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err8 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      const _errs14 = errors;
      if (data2 !== null) {
        const err10 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err11 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.communication_state !== undefined) {
      let data3 = data.communication_state;
      const _errs17 = errors;
      let valid3 = false;
      const _errs18 = errors;
      if (
        !validate198(data3, {
          instancePath: instancePath + '/communication_state',
          parentData: data,
          parentDataProperty: 'communication_state',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate198.errors : vErrors.concat(validate198.errors);
        errors = vErrors.length;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      const _errs19 = errors;
      if (data3 !== null) {
        const err12 = {
          instancePath: instancePath + '/communication_state',
          schemaPath: '#/properties/communication_state/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid2 = _errs19 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err13 = {
          instancePath: instancePath + '/communication_state',
          schemaPath: '#/properties/communication_state/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      } else {
        errors = _errs17;
        if (vErrors !== null) {
          if (_errs17) {
            vErrors.length = _errs17;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.warning !== undefined) {
      let data4 = data.warning;
      const _errs22 = errors;
      let valid4 = false;
      const _errs23 = errors;
      if (
        !validate200(data4, {
          instancePath: instancePath + '/warning',
          parentData: data,
          parentDataProperty: 'warning',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate200.errors : vErrors.concat(validate200.errors);
        errors = vErrors.length;
      }
      var _valid3 = _errs23 === errors;
      valid4 = valid4 || _valid3;
      const _errs24 = errors;
      if (data4 !== null) {
        const err14 = {
          instancePath: instancePath + '/warning',
          schemaPath: '#/properties/warning/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err15 = {
          instancePath: instancePath + '/warning',
          schemaPath: '#/properties/warning/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      } else {
        errors = _errs22;
        if (vErrors !== null) {
          if (_errs22) {
            vErrors.length = _errs22;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err16 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err16];
    } else {
      vErrors.push(err16);
    }
    errors++;
  }
  validate197.errors = vErrors;
  return errors === 0;
}
validate197.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_labels_labels_get = validate202;
const schema113 = {
  type: 'object',
  properties: {
    from: { type: 'string' },
    to: { type: 'string' },
    zone_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    family: { anyOf: [{ type: 'string', pattern: '^[a-z][a-z0-9_]{0,63}$' }, { type: 'null' }] },
    reason_category: {
      anyOf: [{ type: 'string', pattern: '^[a-z][a-z0-9_]{0,63}$' }, { type: 'null' }],
    },
    page_size: { type: 'integer', maximum: 200, minimum: 1 },
    after_labeled_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    after_label_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  required: ['from', 'to'],
  additionalProperties: false,
};

function validate202(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate202.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.from === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'from' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.to === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'to' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'from' ||
        key0 === 'to' ||
        key0 === 'zone_id' ||
        key0 === 'family' ||
        key0 === 'reason_category' ||
        key0 === 'page_size' ||
        key0 === 'after_labeled_at' ||
        key0 === 'after_label_id'
      )) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.from !== undefined) {
      if (typeof data.from !== 'string') {
        const err3 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.to !== undefined) {
      if (typeof data.to !== 'string') {
        const err4 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data2 = data.zone_id;
      const _errs7 = errors;
      let valid1 = false;
      const _errs8 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err5 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      const _errs10 = errors;
      if (data2 !== null) {
        const err7 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err8 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      } else {
        errors = _errs7;
        if (vErrors !== null) {
          if (_errs7) {
            vErrors.length = _errs7;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.family !== undefined) {
      let data3 = data.family;
      const _errs13 = errors;
      let valid2 = false;
      const _errs14 = errors;
      if (typeof data3 === 'string') {
        if (!pattern7.test(data3)) {
          const err9 = {
            instancePath: instancePath + '/family',
            schemaPath: '#/properties/family/anyOf/0/pattern',
            keyword: 'pattern',
            params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
      } else {
        const err10 = {
          instancePath: instancePath + '/family',
          schemaPath: '#/properties/family/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      const _errs16 = errors;
      if (data3 !== null) {
        const err11 = {
          instancePath: instancePath + '/family',
          schemaPath: '#/properties/family/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid1 = _errs16 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err12 = {
          instancePath: instancePath + '/family',
          schemaPath: '#/properties/family/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_category !== undefined) {
      let data4 = data.reason_category;
      const _errs19 = errors;
      let valid3 = false;
      const _errs20 = errors;
      if (typeof data4 === 'string') {
        if (!pattern7.test(data4)) {
          const err13 = {
            instancePath: instancePath + '/reason_category',
            schemaPath: '#/properties/reason_category/anyOf/0/pattern',
            keyword: 'pattern',
            params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid2 = _errs20 === errors;
      valid3 = valid3 || _valid2;
      const _errs22 = errors;
      if (data4 !== null) {
        const err15 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid2 = _errs22 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err16 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      } else {
        errors = _errs19;
        if (vErrors !== null) {
          if (_errs19) {
            vErrors.length = _errs19;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.page_size !== undefined) {
      let data5 = data.page_size;
      if (!(typeof data5 == 'number' && !(data5 % 1) && !isNaN(data5) && isFinite(data5))) {
        const err17 = {
          instancePath: instancePath + '/page_size',
          schemaPath: '#/properties/page_size/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      if (typeof data5 == 'number' && isFinite(data5)) {
        if (data5 > 200 || isNaN(data5)) {
          const err18 = {
            instancePath: instancePath + '/page_size',
            schemaPath: '#/properties/page_size/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err18];
          } else {
            vErrors.push(err18);
          }
          errors++;
        }
        if (data5 < 1 || isNaN(data5)) {
          const err19 = {
            instancePath: instancePath + '/page_size',
            schemaPath: '#/properties/page_size/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err19];
          } else {
            vErrors.push(err19);
          }
          errors++;
        }
      }
    }
    if (data.after_labeled_at !== undefined) {
      let data6 = data.after_labeled_at;
      const _errs27 = errors;
      let valid4 = false;
      const _errs28 = errors;
      if (typeof data6 !== 'string') {
        const err20 = {
          instancePath: instancePath + '/after_labeled_at',
          schemaPath: '#/properties/after_labeled_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid3 = _errs28 === errors;
      valid4 = valid4 || _valid3;
      const _errs30 = errors;
      if (data6 !== null) {
        const err21 = {
          instancePath: instancePath + '/after_labeled_at',
          schemaPath: '#/properties/after_labeled_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      }
      var _valid3 = _errs30 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err22 = {
          instancePath: instancePath + '/after_labeled_at',
          schemaPath: '#/properties/after_labeled_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err22];
        } else {
          vErrors.push(err22);
        }
        errors++;
      } else {
        errors = _errs27;
        if (vErrors !== null) {
          if (_errs27) {
            vErrors.length = _errs27;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.after_label_id !== undefined) {
      let data7 = data.after_label_id;
      const _errs33 = errors;
      let valid5 = false;
      const _errs34 = errors;
      if (typeof data7 === 'string') {
        if (!formats0.test(data7)) {
          const err23 = {
            instancePath: instancePath + '/after_label_id',
            schemaPath: '#/properties/after_label_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err23];
          } else {
            vErrors.push(err23);
          }
          errors++;
        }
      } else {
        const err24 = {
          instancePath: instancePath + '/after_label_id',
          schemaPath: '#/properties/after_label_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err24];
        } else {
          vErrors.push(err24);
        }
        errors++;
      }
      var _valid4 = _errs34 === errors;
      valid5 = valid5 || _valid4;
      const _errs36 = errors;
      if (data7 !== null) {
        const err25 = {
          instancePath: instancePath + '/after_label_id',
          schemaPath: '#/properties/after_label_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err25];
        } else {
          vErrors.push(err25);
        }
        errors++;
      }
      var _valid4 = _errs36 === errors;
      valid5 = valid5 || _valid4;
      if (!valid5) {
        const err26 = {
          instancePath: instancePath + '/after_label_id',
          schemaPath: '#/properties/after_label_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err26];
        } else {
          vErrors.push(err26);
        }
        errors++;
      } else {
        errors = _errs33;
        if (vErrors !== null) {
          if (_errs33) {
            vErrors.length = _errs33;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err27 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err27];
    } else {
      vErrors.push(err27);
    }
    errors++;
  }
  validate202.errors = vErrors;
  return errors === 0;
}
validate202.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_notifications_notifications_get = validate203;
const schema114 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
    status: {
      anyOf: [{ $ref: 'components.json#/$defs/NotificationStatusFilter' }, { type: 'null' }],
    },
    since: { anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }] },
    until: { anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }] },
    include_archived: { type: 'boolean' },
  },
  additionalProperties: false,
};
const schema115 = { type: 'string', enum: ['unread', 'read'] };

function validate204(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate204.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'unread' || data === 'read')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema115.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate204.errors = vErrors;
  return errors === 0;
}
validate204.evaluated = { dynamicProps: false, dynamicItems: false };

function validate203(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate203.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'after' ||
        key0 === 'limit' ||
        key0 === 'status' ||
        key0 === 'since' ||
        key0 === 'until' ||
        key0 === 'include_archived'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
    if (data.status !== undefined) {
      let data2 = data.status;
      const _errs11 = errors;
      let valid2 = false;
      const _errs12 = errors;
      if (
        !validate204(data2, {
          instancePath: instancePath + '/status',
          parentData: data,
          parentDataProperty: 'status',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate204.errors : vErrors.concat(validate204.errors);
        errors = vErrors.length;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      const _errs13 = errors;
      if (data2 !== null) {
        const err8 = {
          instancePath: instancePath + '/status',
          schemaPath: '#/properties/status/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid1 = _errs13 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err9 = {
          instancePath: instancePath + '/status',
          schemaPath: '#/properties/status/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.since !== undefined) {
      let data3 = data.since;
      const _errs16 = errors;
      let valid3 = false;
      const _errs17 = errors;
      if (typeof data3 === 'string') {
        if (!formats16.validate(data3)) {
          const err10 = {
            instancePath: instancePath + '/since',
            schemaPath: '#/properties/since/anyOf/0/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/since',
          schemaPath: '#/properties/since/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid2 = _errs17 === errors;
      valid3 = valid3 || _valid2;
      const _errs19 = errors;
      if (data3 !== null) {
        const err12 = {
          instancePath: instancePath + '/since',
          schemaPath: '#/properties/since/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid2 = _errs19 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err13 = {
          instancePath: instancePath + '/since',
          schemaPath: '#/properties/since/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      } else {
        errors = _errs16;
        if (vErrors !== null) {
          if (_errs16) {
            vErrors.length = _errs16;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.until !== undefined) {
      let data4 = data.until;
      const _errs22 = errors;
      let valid4 = false;
      const _errs23 = errors;
      if (typeof data4 === 'string') {
        if (!formats16.validate(data4)) {
          const err14 = {
            instancePath: instancePath + '/until',
            schemaPath: '#/properties/until/anyOf/0/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err14];
          } else {
            vErrors.push(err14);
          }
          errors++;
        }
      } else {
        const err15 = {
          instancePath: instancePath + '/until',
          schemaPath: '#/properties/until/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid3 = _errs23 === errors;
      valid4 = valid4 || _valid3;
      const _errs25 = errors;
      if (data4 !== null) {
        const err16 = {
          instancePath: instancePath + '/until',
          schemaPath: '#/properties/until/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid3 = _errs25 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err17 = {
          instancePath: instancePath + '/until',
          schemaPath: '#/properties/until/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      } else {
        errors = _errs22;
        if (vErrors !== null) {
          if (_errs22) {
            vErrors.length = _errs22;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.include_archived !== undefined) {
      if (typeof data.include_archived !== 'boolean') {
        const err18 = {
          instancePath: instancePath + '/include_archived',
          schemaPath: '#/properties/include_archived/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      }
    }
  } else {
    const err19 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err19];
    } else {
      vErrors.push(err19);
    }
    errors++;
  }
  validate203.errors = vErrors;
  return errors === 0;
}
validate203.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_overdue_loop_overdue_get = validate206;
const schema116 = {
  type: 'object',
  properties: {
    plant_id: { type: 'string', format: 'uuid' },
    zone_id: {
      anyOf: [{ type: 'array', items: { type: 'string', format: 'uuid' } }, { type: 'null' }],
    },
    from: { anyOf: [{ type: 'string', maxLength: 40 }, { type: 'null' }] },
    to: { anyOf: [{ type: 'string', maxLength: 40 }, { type: 'null' }] },
    min_overdue_days: { type: 'integer', maximum: 3660, minimum: 0 },
    cursor: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  required: ['plant_id'],
  additionalProperties: false,
};

function validate206(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate206.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.plant_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'plant_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'plant_id' ||
        key0 === 'zone_id' ||
        key0 === 'from' ||
        key0 === 'to' ||
        key0 === 'min_overdue_days' ||
        key0 === 'cursor' ||
        key0 === 'limit'
      )) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data0 = data.plant_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err2 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data1 = data.zone_id;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (Array.isArray(data1)) {
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data2 = data1[i0];
          if (typeof data2 === 'string') {
            if (!formats0.test(data2)) {
              const err4 = {
                instancePath: instancePath + '/zone_id/' + i0,
                schemaPath: '#/properties/zone_id/anyOf/0/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err4];
              } else {
                vErrors.push(err4);
              }
              errors++;
            }
          } else {
            const err5 = {
              instancePath: instancePath + '/zone_id/' + i0,
              schemaPath: '#/properties/zone_id/anyOf/0/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err5];
            } else {
              vErrors.push(err5);
            }
            errors++;
          }
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs10 = errors;
      if (data1 !== null) {
        const err7 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err8 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.from !== undefined) {
      let data3 = data.from;
      const _errs13 = errors;
      let valid4 = false;
      const _errs14 = errors;
      if (typeof data3 === 'string') {
        if (func1(data3) > 40) {
          const err9 = {
            instancePath: instancePath + '/from',
            schemaPath: '#/properties/from/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 40 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
      } else {
        const err10 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid4 = valid4 || _valid1;
      const _errs16 = errors;
      if (data3 !== null) {
        const err11 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid1 = _errs16 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err12 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.to !== undefined) {
      let data4 = data.to;
      const _errs19 = errors;
      let valid5 = false;
      const _errs20 = errors;
      if (typeof data4 === 'string') {
        if (func1(data4) > 40) {
          const err13 = {
            instancePath: instancePath + '/to',
            schemaPath: '#/properties/to/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 40 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid2 = _errs20 === errors;
      valid5 = valid5 || _valid2;
      const _errs22 = errors;
      if (data4 !== null) {
        const err15 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid2 = _errs22 === errors;
      valid5 = valid5 || _valid2;
      if (!valid5) {
        const err16 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      } else {
        errors = _errs19;
        if (vErrors !== null) {
          if (_errs19) {
            vErrors.length = _errs19;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.min_overdue_days !== undefined) {
      let data5 = data.min_overdue_days;
      if (!(typeof data5 == 'number' && !(data5 % 1) && !isNaN(data5) && isFinite(data5))) {
        const err17 = {
          instancePath: instancePath + '/min_overdue_days',
          schemaPath: '#/properties/min_overdue_days/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      if (typeof data5 == 'number' && isFinite(data5)) {
        if (data5 > 3660 || isNaN(data5)) {
          const err18 = {
            instancePath: instancePath + '/min_overdue_days',
            schemaPath: '#/properties/min_overdue_days/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 3660 },
          };
          if (vErrors === null) {
            vErrors = [err18];
          } else {
            vErrors.push(err18);
          }
          errors++;
        }
        if (data5 < 0 || isNaN(data5)) {
          const err19 = {
            instancePath: instancePath + '/min_overdue_days',
            schemaPath: '#/properties/min_overdue_days/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 0 },
          };
          if (vErrors === null) {
            vErrors = [err19];
          } else {
            vErrors.push(err19);
          }
          errors++;
        }
      }
    }
    if (data.cursor !== undefined) {
      let data6 = data.cursor;
      const _errs27 = errors;
      let valid6 = false;
      const _errs28 = errors;
      if (typeof data6 === 'string') {
        if (func1(data6) > 128) {
          const err20 = {
            instancePath: instancePath + '/cursor',
            schemaPath: '#/properties/cursor/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err20];
          } else {
            vErrors.push(err20);
          }
          errors++;
        }
      } else {
        const err21 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      }
      var _valid3 = _errs28 === errors;
      valid6 = valid6 || _valid3;
      const _errs30 = errors;
      if (data6 !== null) {
        const err22 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err22];
        } else {
          vErrors.push(err22);
        }
        errors++;
      }
      var _valid3 = _errs30 === errors;
      valid6 = valid6 || _valid3;
      if (!valid6) {
        const err23 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err23];
        } else {
          vErrors.push(err23);
        }
        errors++;
      } else {
        errors = _errs27;
        if (vErrors !== null) {
          if (_errs27) {
            vErrors.length = _errs27;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data7 = data.limit;
      if (!(typeof data7 == 'number' && !(data7 % 1) && !isNaN(data7) && isFinite(data7))) {
        const err24 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err24];
        } else {
          vErrors.push(err24);
        }
        errors++;
      }
      if (typeof data7 == 'number' && isFinite(data7)) {
        if (data7 > 200 || isNaN(data7)) {
          const err25 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err25];
          } else {
            vErrors.push(err25);
          }
          errors++;
        }
        if (data7 < 1 || isNaN(data7)) {
          const err26 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err26];
          } else {
            vErrors.push(err26);
          }
          errors++;
        }
      }
    }
  } else {
    const err27 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err27];
    } else {
      vErrors.push(err27);
    }
    errors++;
  }
  validate206.errors = vErrors;
  return errors === 0;
}
validate206.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_queries_concessions__concession_id__queries_get = validate207;
const schema117 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate207(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate207.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'after' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate207.errors = vErrors;
  return errors === 0;
}
validate207.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_records_ledger_records_get = validate208;
const schema118 = {
  type: 'object',
  properties: {
    record_type: {
      anyOf: [
        {
          type: 'array',
          items: { type: 'string', pattern: '^[a-z][a-z0-9_]{0,63}$' },
          maxItems: 32,
        },
        { type: 'null' },
      ],
    },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    zone_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    node_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    received_from: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    received_before: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    page_size: { type: 'integer', maximum: 200, minimum: 1 },
    after_received_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    after_record_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
};

function validate208(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate208.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!func27.call(schema118.properties, key0)) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.record_type !== undefined) {
      let data0 = data.record_type;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (Array.isArray(data0)) {
        if (data0.length > 32) {
          const err1 = {
            instancePath: instancePath + '/record_type',
            schemaPath: '#/properties/record_type/anyOf/0/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data1 = data0[i0];
          if (typeof data1 === 'string') {
            if (!pattern7.test(data1)) {
              const err2 = {
                instancePath: instancePath + '/record_type/' + i0,
                schemaPath: '#/properties/record_type/anyOf/0/items/pattern',
                keyword: 'pattern',
                params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
              };
              if (vErrors === null) {
                vErrors = [err2];
              } else {
                vErrors.push(err2);
              }
              errors++;
            }
          } else {
            const err3 = {
              instancePath: instancePath + '/record_type/' + i0,
              schemaPath: '#/properties/record_type/anyOf/0/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err3];
            } else {
              vErrors.push(err3);
            }
            errors++;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/record_type',
          schemaPath: '#/properties/record_type/anyOf/0/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data0 !== null) {
        const err5 = {
          instancePath: instancePath + '/record_type',
          schemaPath: '#/properties/record_type/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err6 = {
          instancePath: instancePath + '/record_type',
          schemaPath: '#/properties/record_type/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.plant_id !== undefined) {
      let data2 = data.plant_id;
      const _errs11 = errors;
      let valid4 = false;
      const _errs12 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err7 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid4 = valid4 || _valid1;
      const _errs14 = errors;
      if (data2 !== null) {
        const err9 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err10 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.zone_id !== undefined) {
      let data3 = data.zone_id;
      const _errs17 = errors;
      let valid5 = false;
      const _errs18 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err11 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err11];
          } else {
            vErrors.push(err11);
          }
          errors++;
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid5 = valid5 || _valid2;
      const _errs20 = errors;
      if (data3 !== null) {
        const err13 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid2 = _errs20 === errors;
      valid5 = valid5 || _valid2;
      if (!valid5) {
        const err14 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      } else {
        errors = _errs17;
        if (vErrors !== null) {
          if (_errs17) {
            vErrors.length = _errs17;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.node_id !== undefined) {
      let data4 = data.node_id;
      const _errs23 = errors;
      let valid6 = false;
      const _errs24 = errors;
      if (typeof data4 === 'string') {
        if (!formats0.test(data4)) {
          const err15 = {
            instancePath: instancePath + '/node_id',
            schemaPath: '#/properties/node_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
      } else {
        const err16 = {
          instancePath: instancePath + '/node_id',
          schemaPath: '#/properties/node_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid6 = valid6 || _valid3;
      const _errs26 = errors;
      if (data4 !== null) {
        const err17 = {
          instancePath: instancePath + '/node_id',
          schemaPath: '#/properties/node_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      var _valid3 = _errs26 === errors;
      valid6 = valid6 || _valid3;
      if (!valid6) {
        const err18 = {
          instancePath: instancePath + '/node_id',
          schemaPath: '#/properties/node_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      } else {
        errors = _errs23;
        if (vErrors !== null) {
          if (_errs23) {
            vErrors.length = _errs23;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.received_from !== undefined) {
      let data5 = data.received_from;
      const _errs29 = errors;
      let valid7 = false;
      const _errs30 = errors;
      if (typeof data5 !== 'string') {
        const err19 = {
          instancePath: instancePath + '/received_from',
          schemaPath: '#/properties/received_from/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid7 = valid7 || _valid4;
      const _errs32 = errors;
      if (data5 !== null) {
        const err20 = {
          instancePath: instancePath + '/received_from',
          schemaPath: '#/properties/received_from/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid4 = _errs32 === errors;
      valid7 = valid7 || _valid4;
      if (!valid7) {
        const err21 = {
          instancePath: instancePath + '/received_from',
          schemaPath: '#/properties/received_from/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      } else {
        errors = _errs29;
        if (vErrors !== null) {
          if (_errs29) {
            vErrors.length = _errs29;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.received_before !== undefined) {
      let data6 = data.received_before;
      const _errs35 = errors;
      let valid8 = false;
      const _errs36 = errors;
      if (typeof data6 !== 'string') {
        const err22 = {
          instancePath: instancePath + '/received_before',
          schemaPath: '#/properties/received_before/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err22];
        } else {
          vErrors.push(err22);
        }
        errors++;
      }
      var _valid5 = _errs36 === errors;
      valid8 = valid8 || _valid5;
      const _errs38 = errors;
      if (data6 !== null) {
        const err23 = {
          instancePath: instancePath + '/received_before',
          schemaPath: '#/properties/received_before/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err23];
        } else {
          vErrors.push(err23);
        }
        errors++;
      }
      var _valid5 = _errs38 === errors;
      valid8 = valid8 || _valid5;
      if (!valid8) {
        const err24 = {
          instancePath: instancePath + '/received_before',
          schemaPath: '#/properties/received_before/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err24];
        } else {
          vErrors.push(err24);
        }
        errors++;
      } else {
        errors = _errs35;
        if (vErrors !== null) {
          if (_errs35) {
            vErrors.length = _errs35;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.page_size !== undefined) {
      let data7 = data.page_size;
      if (!(typeof data7 == 'number' && !(data7 % 1) && !isNaN(data7) && isFinite(data7))) {
        const err25 = {
          instancePath: instancePath + '/page_size',
          schemaPath: '#/properties/page_size/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err25];
        } else {
          vErrors.push(err25);
        }
        errors++;
      }
      if (typeof data7 == 'number' && isFinite(data7)) {
        if (data7 > 200 || isNaN(data7)) {
          const err26 = {
            instancePath: instancePath + '/page_size',
            schemaPath: '#/properties/page_size/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err26];
          } else {
            vErrors.push(err26);
          }
          errors++;
        }
        if (data7 < 1 || isNaN(data7)) {
          const err27 = {
            instancePath: instancePath + '/page_size',
            schemaPath: '#/properties/page_size/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err27];
          } else {
            vErrors.push(err27);
          }
          errors++;
        }
      }
    }
    if (data.after_received_at !== undefined) {
      let data8 = data.after_received_at;
      const _errs43 = errors;
      let valid9 = false;
      const _errs44 = errors;
      if (typeof data8 !== 'string') {
        const err28 = {
          instancePath: instancePath + '/after_received_at',
          schemaPath: '#/properties/after_received_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err28];
        } else {
          vErrors.push(err28);
        }
        errors++;
      }
      var _valid6 = _errs44 === errors;
      valid9 = valid9 || _valid6;
      const _errs46 = errors;
      if (data8 !== null) {
        const err29 = {
          instancePath: instancePath + '/after_received_at',
          schemaPath: '#/properties/after_received_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err29];
        } else {
          vErrors.push(err29);
        }
        errors++;
      }
      var _valid6 = _errs46 === errors;
      valid9 = valid9 || _valid6;
      if (!valid9) {
        const err30 = {
          instancePath: instancePath + '/after_received_at',
          schemaPath: '#/properties/after_received_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err30];
        } else {
          vErrors.push(err30);
        }
        errors++;
      } else {
        errors = _errs43;
        if (vErrors !== null) {
          if (_errs43) {
            vErrors.length = _errs43;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.after_record_id !== undefined) {
      let data9 = data.after_record_id;
      const _errs49 = errors;
      let valid10 = false;
      const _errs50 = errors;
      if (typeof data9 === 'string') {
        if (!formats0.test(data9)) {
          const err31 = {
            instancePath: instancePath + '/after_record_id',
            schemaPath: '#/properties/after_record_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err31];
          } else {
            vErrors.push(err31);
          }
          errors++;
        }
      } else {
        const err32 = {
          instancePath: instancePath + '/after_record_id',
          schemaPath: '#/properties/after_record_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err32];
        } else {
          vErrors.push(err32);
        }
        errors++;
      }
      var _valid7 = _errs50 === errors;
      valid10 = valid10 || _valid7;
      const _errs52 = errors;
      if (data9 !== null) {
        const err33 = {
          instancePath: instancePath + '/after_record_id',
          schemaPath: '#/properties/after_record_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err33];
        } else {
          vErrors.push(err33);
        }
        errors++;
      }
      var _valid7 = _errs52 === errors;
      valid10 = valid10 || _valid7;
      if (!valid10) {
        const err34 = {
          instancePath: instancePath + '/after_record_id',
          schemaPath: '#/properties/after_record_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err34];
        } else {
          vErrors.push(err34);
        }
        errors++;
      } else {
        errors = _errs49;
        if (vErrors !== null) {
          if (_errs49) {
            vErrors.length = _errs49;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err35 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err35];
    } else {
      vErrors.push(err35);
    }
    errors++;
  }
  validate208.errors = vErrors;
  return errors === 0;
}
validate208.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_review_queue_loop_review_queue_get = validate209;
const schema119 = {
  type: 'object',
  properties: {
    cursor: { anyOf: [{ type: 'string', maxLength: 128 }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate209(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate209.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'cursor' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.cursor !== undefined) {
      let data0 = data.cursor;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 128) {
          const err1 = {
            instancePath: instancePath + '/cursor',
            schemaPath: '#/properties/cursor/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/cursor',
          schemaPath: '#/properties/cursor/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate209.errors = vErrors;
  return errors === 0;
}
validate209.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__list_users_users_get = validate210;
const schema120 = {
  type: 'object',
  properties: {
    after: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    limit: { type: 'integer', maximum: 200, minimum: 1 },
  },
  additionalProperties: false,
};

function validate210(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate210.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'after' || key0 === 'limit')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.after !== undefined) {
      let data0 = data.after;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err1 = {
            instancePath: instancePath + '/after',
            schemaPath: '#/properties/after/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
      } else {
        const err2 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err3 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err4 = {
          instancePath: instancePath + '/after',
          schemaPath: '#/properties/after/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.limit !== undefined) {
      let data1 = data.limit;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/limit',
          schemaPath: '#/properties/limit/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 200 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 200 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/limit',
            schemaPath: '#/properties/limit/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate210.errors = vErrors;
  return errors === 0;
}
validate210.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__login_auth_login_post = validate211;
const schema121 = { $ref: 'components.json#/$defs/LoginRequest' };
const schema122 = {
  properties: {
    email: { type: 'string', maxLength: 254, minLength: 1 },
    password: { type: 'string', maxLength: 128, minLength: 1 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['email', 'password'],
};

function validate212(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate212.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.email === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'email' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.password === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'password' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'email' || key0 === 'password')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.email !== undefined) {
      let data0 = data.email;
      if (typeof data0 === 'string') {
        if (func1(data0) > 254) {
          const err3 = {
            instancePath: instancePath + '/email',
            schemaPath: '#/properties/email/maxLength',
            keyword: 'maxLength',
            params: { limit: 254 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err4 = {
            instancePath: instancePath + '/email',
            schemaPath: '#/properties/email/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/email',
          schemaPath: '#/properties/email/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.password !== undefined) {
      let data1 = data.password;
      if (typeof data1 === 'string') {
        if (func1(data1) > 128) {
          const err6 = {
            instancePath: instancePath + '/password',
            schemaPath: '#/properties/password/maxLength',
            keyword: 'maxLength',
            params: { limit: 128 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err7 = {
            instancePath: instancePath + '/password',
            schemaPath: '#/properties/password/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/password',
          schemaPath: '#/properties/password/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate212.errors = vErrors;
  return errors === 0;
}
validate212.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate211(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate211.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate212(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate212.errors : vErrors.concat(validate212.errors);
    errors = vErrors.length;
  }
  validate211.errors = vErrors;
  return errors === 0;
}
validate211.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__metrics_accreditation_metrics_get = validate214;
const schema123 = {
  type: 'object',
  properties: {
    period: { type: 'string', pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
    zone_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  required: ['period'],
  additionalProperties: false,
};

function validate214(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate214.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.period === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'period' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'period' || key0 === 'zone_id' || key0 === 'plant_id')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.period !== undefined) {
      let data0 = data.period;
      if (typeof data0 === 'string') {
        if (!pattern4.test(data0)) {
          const err2 = {
            instancePath: instancePath + '/period',
            schemaPath: '#/properties/period/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$' },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/period',
          schemaPath: '#/properties/period/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.zone_id !== undefined) {
      let data1 = data.zone_id;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err4 = {
            instancePath: instancePath + '/zone_id',
            schemaPath: '#/properties/zone_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data1 !== null) {
        const err6 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err7 = {
          instancePath: instancePath + '/zone_id',
          schemaPath: '#/properties/zone_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.plant_id !== undefined) {
      let data2 = data.plant_id;
      const _errs11 = errors;
      let valid2 = false;
      const _errs12 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err8 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      const _errs14 = errors;
      if (data2 !== null) {
        const err10 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err11 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs11;
        if (vErrors !== null) {
          if (_errs11) {
            vErrors.length = _errs11;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err12 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err12];
    } else {
      vErrors.push(err12);
    }
    errors++;
  }
  validate214.errors = vErrors;
  return errors === 0;
}
validate214.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__minimum_coverage_zones__zone_id__minimum_coverage_put = validate215;
const schema124 = { $ref: 'components.json#/$defs/MinimumCoverageBody' };
const schema125 = {
  properties: {
    required_count: { type: 'integer' },
    required_camera_ids: { items: { type: 'string', format: 'uuid' }, type: 'array', maxItems: 8 },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['required_count', 'required_camera_ids', 'reason_es'],
};

function validate216(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate216.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.required_count === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'required_count' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.required_camera_ids === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'required_camera_ids' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'required_count' || key0 === 'required_camera_ids' || key0 === 'reason_es')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.required_count !== undefined) {
      let data0 = data.required_count;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err4 = {
          instancePath: instancePath + '/required_count',
          schemaPath: '#/properties/required_count/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.required_camera_ids !== undefined) {
      let data1 = data.required_camera_ids;
      if (Array.isArray(data1)) {
        if (data1.length > 8) {
          const err5 = {
            instancePath: instancePath + '/required_camera_ids',
            schemaPath: '#/properties/required_camera_ids/maxItems',
            keyword: 'maxItems',
            params: { limit: 8 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data2 = data1[i0];
          if (typeof data2 === 'string') {
            if (!formats0.test(data2)) {
              const err6 = {
                instancePath: instancePath + '/required_camera_ids/' + i0,
                schemaPath: '#/properties/required_camera_ids/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err6];
              } else {
                vErrors.push(err6);
              }
              errors++;
            }
          } else {
            const err7 = {
              instancePath: instancePath + '/required_camera_ids/' + i0,
              schemaPath: '#/properties/required_camera_ids/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err7];
            } else {
              vErrors.push(err7);
            }
            errors++;
          }
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/required_camera_ids',
          schemaPath: '#/properties/required_camera_ids/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err9 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
  } else {
    const err10 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err10];
    } else {
      vErrors.push(err10);
    }
    errors++;
  }
  validate216.errors = vErrors;
  return errors === 0;
}
validate216.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate215(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate215.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate216(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate216.errors : vErrors.concat(validate216.errors);
    errors = vErrors.length;
  }
  validate215.errors = vErrors;
  return errors === 0;
}
validate215.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__new_standard_version_zones__zone_id__standards__standard_id__versions_post =
  validate218;
const schema126 = { $ref: 'components.json#/$defs/StandardVersionBody' };
const schema127 = {
  properties: {
    changes: { $ref: 'components.json#/$defs/StandardChanges' },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['changes', 'reason_es'],
};
const schema128 = {
  properties: {
    title_es: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    declared_text: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    predicate: { anyOf: [{ additionalProperties: true, type: 'object' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};

function validate220(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate220.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'title_es' || key0 === 'declared_text' || key0 === 'predicate')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.title_es !== undefined) {
      let data0 = data.title_es;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/title_es',
          schemaPath: '#/properties/title_es/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/title_es',
          schemaPath: '#/properties/title_es/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/title_es',
          schemaPath: '#/properties/title_es/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.declared_text !== undefined) {
      let data1 = data.declared_text;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/declared_text',
          schemaPath: '#/properties/declared_text/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/declared_text',
          schemaPath: '#/properties/declared_text/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/declared_text',
          schemaPath: '#/properties/declared_text/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.predicate !== undefined) {
      let data2 = data.predicate;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (data2 && typeof data2 == 'object' && !Array.isArray(data2)) {
      } else {
        const err7 = {
          instancePath: instancePath + '/predicate',
          schemaPath: '#/properties/predicate/anyOf/0/type',
          keyword: 'type',
          params: { type: 'object' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs19 = errors;
      if (data2 !== null) {
        const err8 = {
          instancePath: instancePath + '/predicate',
          schemaPath: '#/properties/predicate/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs19 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err9 = {
          instancePath: instancePath + '/predicate',
          schemaPath: '#/properties/predicate/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err10 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err10];
    } else {
      vErrors.push(err10);
    }
    errors++;
  }
  validate220.errors = vErrors;
  return errors === 0;
}
validate220.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate219(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate219.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.changes === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'changes' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'changes' || key0 === 'reason_es')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.changes !== undefined) {
      if (
        !validate220(data.changes, {
          instancePath: instancePath + '/changes',
          parentData: data,
          parentDataProperty: 'changes',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate220.errors : vErrors.concat(validate220.errors);
        errors = vErrors.length;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err3 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
  } else {
    const err4 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err4];
    } else {
      vErrors.push(err4);
    }
    errors++;
  }
  validate219.errors = vErrors;
  return errors === 0;
}
validate219.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate218(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate218.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate219(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate219.errors : vErrors.concat(validate219.errors);
    errors = vErrors.length;
  }
  validate218.errors = vErrors;
  return errors === 0;
}
validate218.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__new_standard_zones__zone_id__standards_post = validate223;
const schema129 = { $ref: 'components.json#/$defs/StandardBody' };
const schema130 = {
  properties: {
    family: { $ref: 'components.json#/$defs/PredicateFamily' },
    title_es: { type: 'string' },
    declared_text: { type: 'string' },
    predicate: { additionalProperties: true, type: 'object' },
    reason_es: { type: 'string' },
    zone_parameters: {
      anyOf: [{ $ref: 'components.json#/$defs/ZoneParametersBody' }, { type: 'null' }],
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['family', 'title_es', 'declared_text', 'predicate', 'reason_es'],
};
const schema131 = {
  properties: {
    cameras: {
      items: { $ref: 'components.json#/$defs/CameraBody' },
      type: 'array',
      maxItems: 8,
      minItems: 1,
    },
    minimum_coverage: { $ref: 'components.json#/$defs/CoverageValues' },
    signals: { items: { additionalProperties: true, type: 'object' }, type: 'array', maxItems: 32 },
    thresholds: { $ref: 'components.json#/$defs/ThresholdValues' },
    clip_window: { $ref: 'components.json#/$defs/ClipWindowBody' },
    episode: { $ref: 'components.json#/$defs/EpisodeBody' },
    single_occupancy: { type: 'boolean' },
    aggregation_window_minutes: { type: 'integer', maximum: 480, minimum: 15 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['cameras', 'minimum_coverage', 'signals', 'thresholds', 'clip_window', 'episode'],
};
const schema132 = {
  properties: {
    required_count: { type: 'integer' },
    required_camera_ids: { items: { type: 'string', format: 'uuid' }, type: 'array', maxItems: 8 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['required_count', 'required_camera_ids'],
};

function validate228(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate228.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.required_count === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'required_count' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.required_camera_ids === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'required_camera_ids' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'required_count' || key0 === 'required_camera_ids')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.required_count !== undefined) {
      let data0 = data.required_count;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err3 = {
          instancePath: instancePath + '/required_count',
          schemaPath: '#/properties/required_count/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.required_camera_ids !== undefined) {
      let data1 = data.required_camera_ids;
      if (Array.isArray(data1)) {
        if (data1.length > 8) {
          const err4 = {
            instancePath: instancePath + '/required_camera_ids',
            schemaPath: '#/properties/required_camera_ids/maxItems',
            keyword: 'maxItems',
            params: { limit: 8 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data2 = data1[i0];
          if (typeof data2 === 'string') {
            if (!formats0.test(data2)) {
              const err5 = {
                instancePath: instancePath + '/required_camera_ids/' + i0,
                schemaPath: '#/properties/required_camera_ids/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err5];
              } else {
                vErrors.push(err5);
              }
              errors++;
            }
          } else {
            const err6 = {
              instancePath: instancePath + '/required_camera_ids/' + i0,
              schemaPath: '#/properties/required_camera_ids/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err6];
            } else {
              vErrors.push(err6);
            }
            errors++;
          }
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/required_camera_ids',
          schemaPath: '#/properties/required_camera_ids/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate228.errors = vErrors;
  return errors === 0;
}
validate228.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema133 = {
  properties: {
    review: { type: 'number', exclusiveMaximum: 1, exclusiveMinimum: 0 },
    publication: { type: 'number', maximum: 1, exclusiveMinimum: 0 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['review', 'publication'],
};

function validate230(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate230.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.review === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'review' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.publication === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'publication' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'review' || key0 === 'publication')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.review !== undefined) {
      let data0 = data.review;
      if (typeof data0 == 'number' && isFinite(data0)) {
        if (data0 >= 1 || isNaN(data0)) {
          const err3 = {
            instancePath: instancePath + '/review',
            schemaPath: '#/properties/review/exclusiveMaximum',
            keyword: 'exclusiveMaximum',
            params: { comparison: '<', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        if (data0 <= 0 || isNaN(data0)) {
          const err4 = {
            instancePath: instancePath + '/review',
            schemaPath: '#/properties/review/exclusiveMinimum',
            keyword: 'exclusiveMinimum',
            params: { comparison: '>', limit: 0 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/review',
          schemaPath: '#/properties/review/type',
          keyword: 'type',
          params: { type: 'number' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.publication !== undefined) {
      let data1 = data.publication;
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 1 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/publication',
            schemaPath: '#/properties/publication/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1 <= 0 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/publication',
            schemaPath: '#/properties/publication/exclusiveMinimum',
            keyword: 'exclusiveMinimum',
            params: { comparison: '>', limit: 0 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/publication',
          schemaPath: '#/properties/publication/type',
          keyword: 'type',
          params: { type: 'number' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate230.errors = vErrors;
  return errors === 0;
}
validate230.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema134 = {
  properties: { pre_seconds: { type: 'integer' }, post_seconds: { type: 'integer' } },
  additionalProperties: false,
  type: 'object',
};

function validate232(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate232.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'pre_seconds' || key0 === 'post_seconds')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.pre_seconds !== undefined) {
      let data0 = data.pre_seconds;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err1 = {
          instancePath: instancePath + '/pre_seconds',
          schemaPath: '#/properties/pre_seconds/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.post_seconds !== undefined) {
      let data1 = data.post_seconds;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err2 = {
          instancePath: instancePath + '/post_seconds',
          schemaPath: '#/properties/post_seconds/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate232.errors = vErrors;
  return errors === 0;
}
validate232.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema135 = {
  properties: { grouping_window_ms: { type: 'integer' }, max_segment_ms: { type: 'integer' } },
  additionalProperties: false,
  type: 'object',
};

function validate234(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate234.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'grouping_window_ms' || key0 === 'max_segment_ms')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.grouping_window_ms !== undefined) {
      let data0 = data.grouping_window_ms;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err1 = {
          instancePath: instancePath + '/grouping_window_ms',
          schemaPath: '#/properties/grouping_window_ms/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.max_segment_ms !== undefined) {
      let data1 = data.max_segment_ms;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err2 = {
          instancePath: instancePath + '/max_segment_ms',
          schemaPath: '#/properties/max_segment_ms/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate234.errors = vErrors;
  return errors === 0;
}
validate234.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate226(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate226.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.cameras === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'cameras' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.minimum_coverage === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'minimum_coverage' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.signals === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'signals' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.thresholds === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'thresholds' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.clip_window === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'clip_window' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    if (data.episode === undefined) {
      const err5 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'episode' },
      };
      if (vErrors === null) {
        vErrors = [err5];
      } else {
        vErrors.push(err5);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'cameras' ||
        key0 === 'minimum_coverage' ||
        key0 === 'signals' ||
        key0 === 'thresholds' ||
        key0 === 'clip_window' ||
        key0 === 'episode' ||
        key0 === 'single_occupancy' ||
        key0 === 'aggregation_window_minutes'
      )) {
        const err6 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.cameras !== undefined) {
      let data0 = data.cameras;
      if (Array.isArray(data0)) {
        if (data0.length > 8) {
          const err7 = {
            instancePath: instancePath + '/cameras',
            schemaPath: '#/properties/cameras/maxItems',
            keyword: 'maxItems',
            params: { limit: 8 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (data0.length < 1) {
          const err8 = {
            instancePath: instancePath + '/cameras',
            schemaPath: '#/properties/cameras/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate81(data0[i0], {
              instancePath: instancePath + '/cameras/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate81.errors : vErrors.concat(validate81.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/cameras',
          schemaPath: '#/properties/cameras/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.minimum_coverage !== undefined) {
      if (
        !validate228(data.minimum_coverage, {
          instancePath: instancePath + '/minimum_coverage',
          parentData: data,
          parentDataProperty: 'minimum_coverage',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate228.errors : vErrors.concat(validate228.errors);
        errors = vErrors.length;
      }
    }
    if (data.signals !== undefined) {
      let data3 = data.signals;
      if (Array.isArray(data3)) {
        if (data3.length > 32) {
          const err10 = {
            instancePath: instancePath + '/signals',
            schemaPath: '#/properties/signals/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
        const len1 = data3.length;
        for (let i1 = 0; i1 < len1; i1++) {
          let data4 = data3[i1];
          if (data4 && typeof data4 == 'object' && !Array.isArray(data4)) {
          } else {
            const err11 = {
              instancePath: instancePath + '/signals/' + i1,
              schemaPath: '#/properties/signals/items/type',
              keyword: 'type',
              params: { type: 'object' },
            };
            if (vErrors === null) {
              vErrors = [err11];
            } else {
              vErrors.push(err11);
            }
            errors++;
          }
        }
      } else {
        const err12 = {
          instancePath: instancePath + '/signals',
          schemaPath: '#/properties/signals/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
    }
    if (data.thresholds !== undefined) {
      if (
        !validate230(data.thresholds, {
          instancePath: instancePath + '/thresholds',
          parentData: data,
          parentDataProperty: 'thresholds',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate230.errors : vErrors.concat(validate230.errors);
        errors = vErrors.length;
      }
    }
    if (data.clip_window !== undefined) {
      if (
        !validate232(data.clip_window, {
          instancePath: instancePath + '/clip_window',
          parentData: data,
          parentDataProperty: 'clip_window',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate232.errors : vErrors.concat(validate232.errors);
        errors = vErrors.length;
      }
    }
    if (data.episode !== undefined) {
      if (
        !validate234(data.episode, {
          instancePath: instancePath + '/episode',
          parentData: data,
          parentDataProperty: 'episode',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate234.errors : vErrors.concat(validate234.errors);
        errors = vErrors.length;
      }
    }
    if (data.single_occupancy !== undefined) {
      if (typeof data.single_occupancy !== 'boolean') {
        const err13 = {
          instancePath: instancePath + '/single_occupancy',
          schemaPath: '#/properties/single_occupancy/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
    }
    if (data.aggregation_window_minutes !== undefined) {
      let data9 = data.aggregation_window_minutes;
      if (!(typeof data9 == 'number' && !(data9 % 1) && !isNaN(data9) && isFinite(data9))) {
        const err14 = {
          instancePath: instancePath + '/aggregation_window_minutes',
          schemaPath: '#/properties/aggregation_window_minutes/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      if (typeof data9 == 'number' && isFinite(data9)) {
        if (data9 > 480 || isNaN(data9)) {
          const err15 = {
            instancePath: instancePath + '/aggregation_window_minutes',
            schemaPath: '#/properties/aggregation_window_minutes/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 480 },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
        if (data9 < 15 || isNaN(data9)) {
          const err16 = {
            instancePath: instancePath + '/aggregation_window_minutes',
            schemaPath: '#/properties/aggregation_window_minutes/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 15 },
          };
          if (vErrors === null) {
            vErrors = [err16];
          } else {
            vErrors.push(err16);
          }
          errors++;
        }
      }
    }
  } else {
    const err17 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err17];
    } else {
      vErrors.push(err17);
    }
    errors++;
  }
  validate226.errors = vErrors;
  return errors === 0;
}
validate226.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate224(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate224.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.family === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'family' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.title_es === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'title_es' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.declared_text === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'declared_text' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.predicate === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'predicate' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'family' ||
        key0 === 'title_es' ||
        key0 === 'declared_text' ||
        key0 === 'predicate' ||
        key0 === 'reason_es' ||
        key0 === 'zone_parameters'
      )) {
        const err5 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.family !== undefined) {
      if (
        !validate151(data.family, {
          instancePath: instancePath + '/family',
          parentData: data,
          parentDataProperty: 'family',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate151.errors : vErrors.concat(validate151.errors);
        errors = vErrors.length;
      }
    }
    if (data.title_es !== undefined) {
      if (typeof data.title_es !== 'string') {
        const err6 = {
          instancePath: instancePath + '/title_es',
          schemaPath: '#/properties/title_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.declared_text !== undefined) {
      if (typeof data.declared_text !== 'string') {
        const err7 = {
          instancePath: instancePath + '/declared_text',
          schemaPath: '#/properties/declared_text/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.predicate !== undefined) {
      let data3 = data.predicate;
      if (data3 && typeof data3 == 'object' && !Array.isArray(data3)) {
      } else {
        const err8 = {
          instancePath: instancePath + '/predicate',
          schemaPath: '#/properties/predicate/type',
          keyword: 'type',
          params: { type: 'object' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err9 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.zone_parameters !== undefined) {
      let data5 = data.zone_parameters;
      const _errs13 = errors;
      let valid1 = false;
      const _errs14 = errors;
      if (
        !validate226(data5, {
          instancePath: instancePath + '/zone_parameters',
          parentData: data,
          parentDataProperty: 'zone_parameters',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate226.errors : vErrors.concat(validate226.errors);
        errors = vErrors.length;
      }
      var _valid0 = _errs14 === errors;
      valid1 = valid1 || _valid0;
      const _errs15 = errors;
      if (data5 !== null) {
        const err10 = {
          instancePath: instancePath + '/zone_parameters',
          schemaPath: '#/properties/zone_parameters/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid0 = _errs15 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err11 = {
          instancePath: instancePath + '/zone_parameters',
          schemaPath: '#/properties/zone_parameters/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err12 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err12];
    } else {
      vErrors.push(err12);
    }
    errors++;
  }
  validate224.errors = vErrors;
  return errors === 0;
}
validate224.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate223(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate223.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate224(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate224.errors : vErrors.concat(validate224.errors);
    errors = vErrors.length;
  }
  validate223.errors = vErrors;
  return errors === 0;
}
validate223.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post =
  validate238;
const schema136 = { $ref: 'components.json#/$defs/RerunBody' };
const schema137 = {
  properties: { passes_per_cell: { type: 'integer' } },
  additionalProperties: false,
  type: 'object',
  required: ['passes_per_cell'],
};

function validate239(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate239.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.passes_per_cell === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'passes_per_cell' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'passes_per_cell')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.passes_per_cell !== undefined) {
      let data0 = data.passes_per_cell;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err2 = {
          instancePath: instancePath + '/passes_per_cell',
          schemaPath: '#/properties/passes_per_cell/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate239.errors = vErrors;
  return errors === 0;
}
validate239.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate238(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate238.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate239(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate239.errors : vErrors.concat(validate239.errors);
    errors = vErrors.length;
  }
  validate238.errors = vErrors;
  return errors === 0;
}
validate238.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__open_walk_test_zones__zone_id__walk_tests_post = validate241;
const schema138 = { $ref: 'components.json#/$defs/OpenWalkTestBody' };
const schema139 = {
  properties: { passes_per_cell: { type: 'integer' } },
  additionalProperties: false,
  type: 'object',
  required: ['passes_per_cell'],
};

function validate242(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate242.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.passes_per_cell === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'passes_per_cell' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'passes_per_cell')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.passes_per_cell !== undefined) {
      let data0 = data.passes_per_cell;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err2 = {
          instancePath: instancePath + '/passes_per_cell',
          schemaPath: '#/properties/passes_per_cell/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate242.errors = vErrors;
  return errors === 0;
}
validate242.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate241(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate241.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate242(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate242.errors : vErrors.concat(validate242.errors);
    errors = vErrors.length;
  }
  validate241.errors = vErrors;
  return errors === 0;
}
validate241.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__publish_target_version_fleet_target_versions_post = validate244;
const schema140 = { $ref: 'components.json#/$defs/TargetVersionBody' };
const schema141 = {
  properties: {
    plant_id: { type: 'string', format: 'uuid' },
    node_ids: {
      anyOf: [
        { items: { type: 'string', format: 'uuid' }, type: 'array', maxItems: 100, minItems: 1 },
        { type: 'null' },
      ],
    },
    group: { anyOf: [{ $ref: 'components.json#/$defs/NodeGroup' }, { type: 'null' }] },
    target_version: { type: 'string', maxLength: 64 },
    maintenance_window: { $ref: 'components.json#/$defs/MaintenanceWindowBody' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['plant_id', 'target_version', 'maintenance_window'],
};
const schema142 = { type: 'string', enum: ['plant'] };

function validate246(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate246.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'plant')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema142.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate246.errors = vErrors;
  return errors === 0;
}
validate246.evaluated = { dynamicProps: false, dynamicItems: false };

const schema143 = {
  properties: {
    from: { type: 'string', format: 'date-time' },
    to: { type: 'string', format: 'date-time' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['from', 'to'],
};

function validate248(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate248.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.from === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'from' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.to === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'to' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'from' || key0 === 'to')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.from !== undefined) {
      let data0 = data.from;
      if (typeof data0 === 'string') {
        if (!formats16.validate(data0)) {
          const err3 = {
            instancePath: instancePath + '/from',
            schemaPath: '#/properties/from/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.to !== undefined) {
      let data1 = data.to;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err5 = {
            instancePath: instancePath + '/to',
            schemaPath: '#/properties/to/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate248.errors = vErrors;
  return errors === 0;
}
validate248.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate245(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate245.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.plant_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'plant_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.target_version === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'target_version' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.maintenance_window === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'maintenance_window' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'plant_id' ||
        key0 === 'node_ids' ||
        key0 === 'group' ||
        key0 === 'target_version' ||
        key0 === 'maintenance_window'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data0 = data.plant_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err4 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.node_ids !== undefined) {
      let data1 = data.node_ids;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (Array.isArray(data1)) {
        if (data1.length > 100) {
          const err6 = {
            instancePath: instancePath + '/node_ids',
            schemaPath: '#/properties/node_ids/anyOf/0/maxItems',
            keyword: 'maxItems',
            params: { limit: 100 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (data1.length < 1) {
          const err7 = {
            instancePath: instancePath + '/node_ids',
            schemaPath: '#/properties/node_ids/anyOf/0/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data2 = data1[i0];
          if (typeof data2 === 'string') {
            if (!formats0.test(data2)) {
              const err8 = {
                instancePath: instancePath + '/node_ids/' + i0,
                schemaPath: '#/properties/node_ids/anyOf/0/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err8];
              } else {
                vErrors.push(err8);
              }
              errors++;
            }
          } else {
            const err9 = {
              instancePath: instancePath + '/node_ids/' + i0,
              schemaPath: '#/properties/node_ids/anyOf/0/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err9];
            } else {
              vErrors.push(err9);
            }
            errors++;
          }
        }
      } else {
        const err10 = {
          instancePath: instancePath + '/node_ids',
          schemaPath: '#/properties/node_ids/anyOf/0/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs10 = errors;
      if (data1 !== null) {
        const err11 = {
          instancePath: instancePath + '/node_ids',
          schemaPath: '#/properties/node_ids/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err12 = {
          instancePath: instancePath + '/node_ids',
          schemaPath: '#/properties/node_ids/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.group !== undefined) {
      let data3 = data.group;
      const _errs13 = errors;
      let valid4 = false;
      const _errs14 = errors;
      if (
        !validate246(data3, {
          instancePath: instancePath + '/group',
          parentData: data,
          parentDataProperty: 'group',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate246.errors : vErrors.concat(validate246.errors);
        errors = vErrors.length;
      }
      var _valid1 = _errs14 === errors;
      valid4 = valid4 || _valid1;
      const _errs15 = errors;
      if (data3 !== null) {
        const err13 = {
          instancePath: instancePath + '/group',
          schemaPath: '#/properties/group/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid1 = _errs15 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err14 = {
          instancePath: instancePath + '/group',
          schemaPath: '#/properties/group/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.target_version !== undefined) {
      let data4 = data.target_version;
      if (typeof data4 === 'string') {
        if (func1(data4) > 64) {
          const err15 = {
            instancePath: instancePath + '/target_version',
            schemaPath: '#/properties/target_version/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err15];
          } else {
            vErrors.push(err15);
          }
          errors++;
        }
      } else {
        const err16 = {
          instancePath: instancePath + '/target_version',
          schemaPath: '#/properties/target_version/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
    }
    if (data.maintenance_window !== undefined) {
      if (
        !validate248(data.maintenance_window, {
          instancePath: instancePath + '/maintenance_window',
          parentData: data,
          parentDataProperty: 'maintenance_window',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate248.errors : vErrors.concat(validate248.errors);
        errors = vErrors.length;
      }
    }
  } else {
    const err17 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err17];
    } else {
      vErrors.push(err17);
    }
    errors++;
  }
  validate245.errors = vErrors;
  return errors === 0;
}
validate245.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate244(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate244.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate245(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate245.errors : vErrors.concat(validate245.errors);
    errors = vErrors.length;
  }
  validate244.errors = vErrors;
  return errors === 0;
}
validate244.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put = validate251;
const schema144 = { $ref: 'components.json#/$defs/FleetThresholdsBody' };
const schema145 = {
  properties: {
    queue_pending_threshold: { type: 'integer' },
    queue_age_threshold_minutes: { type: 'integer' },
    clock_drift_threshold_ms: { type: 'integer' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['queue_pending_threshold', 'queue_age_threshold_minutes', 'clock_drift_threshold_ms'],
};

function validate252(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate252.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.queue_pending_threshold === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'queue_pending_threshold' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.queue_age_threshold_minutes === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'queue_age_threshold_minutes' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.clock_drift_threshold_ms === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'clock_drift_threshold_ms' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'queue_pending_threshold' ||
        key0 === 'queue_age_threshold_minutes' ||
        key0 === 'clock_drift_threshold_ms'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.queue_pending_threshold !== undefined) {
      let data0 = data.queue_pending_threshold;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err4 = {
          instancePath: instancePath + '/queue_pending_threshold',
          schemaPath: '#/properties/queue_pending_threshold/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.queue_age_threshold_minutes !== undefined) {
      let data1 = data.queue_age_threshold_minutes;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err5 = {
          instancePath: instancePath + '/queue_age_threshold_minutes',
          schemaPath: '#/properties/queue_age_threshold_minutes/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.clock_drift_threshold_ms !== undefined) {
      let data2 = data.clock_drift_threshold_ms;
      if (!(typeof data2 == 'number' && !(data2 % 1) && !isNaN(data2) && isFinite(data2))) {
        const err6 = {
          instancePath: instancePath + '/clock_drift_threshold_ms',
          schemaPath: '#/properties/clock_drift_threshold_ms/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate252.errors = vErrors;
  return errors === 0;
}
validate252.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate251(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate251.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate252(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate252.errors : vErrors.concat(validate252.errors);
    errors = vErrors.length;
  }
  validate251.errors = vErrors;
  return errors === 0;
}
validate251.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__put_signatory_policy_plants__plant_id__signatory_policy_put = validate254;
const schema146 = { $ref: 'components.json#/$defs/SignatoryPolicyBody' };
const schema147 = {
  properties: {
    required_roles: { items: { $ref: 'components.json#/$defs/Role' }, type: 'array', maxItems: 7 },
    minimum: { type: 'integer' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['required_roles', 'minimum'],
};

function validate255(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate255.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.required_roles === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'required_roles' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.minimum === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'minimum' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'required_roles' || key0 === 'minimum')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.required_roles !== undefined) {
      let data0 = data.required_roles;
      if (Array.isArray(data0)) {
        if (data0.length > 7) {
          const err3 = {
            instancePath: instancePath + '/required_roles',
            schemaPath: '#/properties/required_roles/maxItems',
            keyword: 'maxItems',
            params: { limit: 7 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate71(data0[i0], {
              instancePath: instancePath + '/required_roles/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate71.errors : vErrors.concat(validate71.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/required_roles',
          schemaPath: '#/properties/required_roles/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.minimum !== undefined) {
      let data2 = data.minimum;
      if (!(typeof data2 == 'number' && !(data2 % 1) && !isNaN(data2) && isFinite(data2))) {
        const err5 = {
          instancePath: instancePath + '/minimum',
          schemaPath: '#/properties/minimum/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate255.errors = vErrors;
  return errors === 0;
}
validate255.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate254(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate254.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate255(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate255.errors : vErrors.concat(validate255.errors);
    errors = vErrors.length;
  }
  validate254.errors = vErrors;
  return errors === 0;
}
validate254.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put =
  validate258;
const schema148 = { $ref: 'components.json#/$defs/AnalysisStepBody' };
const schema149 = {
  properties: {
    statement: { type: 'string' },
    evidence_refs: {
      items: { $ref: 'components.json#/$defs/EvidenceRefBody' },
      type: 'array',
      maxItems: 32,
    },
    category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['statement'],
};
const schema150 = {
  properties: { kind: { type: 'string' }, id: { type: 'string', format: 'uuid' } },
  additionalProperties: false,
  type: 'object',
  required: ['kind', 'id'],
};

function validate260(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate260.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.kind === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'kind' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.id === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'id' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'kind' || key0 === 'id')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.kind !== undefined) {
      if (typeof data.kind !== 'string') {
        const err3 = {
          instancePath: instancePath + '/kind',
          schemaPath: '#/properties/kind/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.id !== undefined) {
      let data1 = data.id;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err4 = {
            instancePath: instancePath + '/id',
            schemaPath: '#/properties/id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/id',
          schemaPath: '#/properties/id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate260.errors = vErrors;
  return errors === 0;
}
validate260.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate259(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate259.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.statement === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'statement' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'statement' || key0 === 'evidence_refs' || key0 === 'category')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.statement !== undefined) {
      if (typeof data.statement !== 'string') {
        const err2 = {
          instancePath: instancePath + '/statement',
          schemaPath: '#/properties/statement/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.evidence_refs !== undefined) {
      let data1 = data.evidence_refs;
      if (Array.isArray(data1)) {
        if (data1.length > 32) {
          const err3 = {
            instancePath: instancePath + '/evidence_refs',
            schemaPath: '#/properties/evidence_refs/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate260(data1[i0], {
              instancePath: instancePath + '/evidence_refs/' + i0,
              parentData: data1,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate260.errors : vErrors.concat(validate260.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/evidence_refs',
          schemaPath: '#/properties/evidence_refs/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.category !== undefined) {
      let data3 = data.category;
      const _errs8 = errors;
      let valid3 = false;
      const _errs9 = errors;
      if (typeof data3 !== 'string') {
        const err5 = {
          instancePath: instancePath + '/category',
          schemaPath: '#/properties/category/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs9 === errors;
      valid3 = valid3 || _valid0;
      const _errs11 = errors;
      if (data3 !== null) {
        const err6 = {
          instancePath: instancePath + '/category',
          schemaPath: '#/properties/category/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs11 === errors;
      valid3 = valid3 || _valid0;
      if (!valid3) {
        const err7 = {
          instancePath: instancePath + '/category',
          schemaPath: '#/properties/category/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      } else {
        errors = _errs8;
        if (vErrors !== null) {
          if (_errs8) {
            vErrors.length = _errs8;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate259.errors = vErrors;
  return errors === 0;
}
validate259.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate258(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate258.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate259(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate259.errors : vErrors.concat(validate259.errors);
    errors = vErrors.length;
  }
  validate258.errors = vErrors;
  return errors === 0;
}
validate258.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__reactivate_users__user_id__reactivate_post = validate263;
const schema151 = { $ref: 'components.json#/$defs/ReactivateBody' };
const schema152 = {
  properties: {
    assignments: {
      items: { $ref: 'components.json#/$defs/AssignmentBody' },
      type: 'array',
      maxItems: 32,
      minItems: 1,
    },
    disclose_link: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['assignments'],
};

function validate264(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate264.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.assignments === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'assignments' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'assignments' || key0 === 'disclose_link')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.assignments !== undefined) {
      let data0 = data.assignments;
      if (Array.isArray(data0)) {
        if (data0.length > 32) {
          const err2 = {
            instancePath: instancePath + '/assignments',
            schemaPath: '#/properties/assignments/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        if (data0.length < 1) {
          const err3 = {
            instancePath: instancePath + '/assignments',
            schemaPath: '#/properties/assignments/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate175(data0[i0], {
              instancePath: instancePath + '/assignments/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate175.errors : vErrors.concat(validate175.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/assignments',
          schemaPath: '#/properties/assignments/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.disclose_link !== undefined) {
      if (typeof data.disclose_link !== 'boolean') {
        const err5 = {
          instancePath: instancePath + '/disclose_link',
          schemaPath: '#/properties/disclose_link/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate264.errors = vErrors;
  return errors === 0;
}
validate264.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate263(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate263.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate264(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate264.errors : vErrors.concat(validate264.errors);
    errors = vErrors.length;
  }
  validate263.errors = vErrors;
  return errors === 0;
}
validate263.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__record_commitments_accreditation_monthly_packages__package_id__commitments_post =
  validate267;
const schema153 = { $ref: 'components.json#/$defs/CommitmentsBody' };
const schema154 = {
  properties: {
    commitments: {
      items: { $ref: 'components.json#/$defs/CommitmentIn' },
      type: 'array',
      maxItems: 20,
      minItems: 1,
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['commitments'],
};
const schema155 = {
  properties: {
    text: { type: 'string' },
    responsible_user_id: { type: 'string', format: 'uuid' },
    due_date: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['text', 'responsible_user_id', 'due_date'],
};

function validate269(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate269.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.text === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'text' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.responsible_user_id === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'responsible_user_id' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.due_date === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'due_date' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'text' || key0 === 'responsible_user_id' || key0 === 'due_date')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.text !== undefined) {
      if (typeof data.text !== 'string') {
        const err4 = {
          instancePath: instancePath + '/text',
          schemaPath: '#/properties/text/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.responsible_user_id !== undefined) {
      let data1 = data.responsible_user_id;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err5 = {
            instancePath: instancePath + '/responsible_user_id',
            schemaPath: '#/properties/responsible_user_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/responsible_user_id',
          schemaPath: '#/properties/responsible_user_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.due_date !== undefined) {
      if (typeof data.due_date !== 'string') {
        const err7 = {
          instancePath: instancePath + '/due_date',
          schemaPath: '#/properties/due_date/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate269.errors = vErrors;
  return errors === 0;
}
validate269.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate268(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate268.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.commitments === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'commitments' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'commitments')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.commitments !== undefined) {
      let data0 = data.commitments;
      if (Array.isArray(data0)) {
        if (data0.length > 20) {
          const err2 = {
            instancePath: instancePath + '/commitments',
            schemaPath: '#/properties/commitments/maxItems',
            keyword: 'maxItems',
            params: { limit: 20 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        if (data0.length < 1) {
          const err3 = {
            instancePath: instancePath + '/commitments',
            schemaPath: '#/properties/commitments/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate269(data0[i0], {
              instancePath: instancePath + '/commitments/' + i0,
              parentData: data0,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate269.errors : vErrors.concat(validate269.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/commitments',
          schemaPath: '#/properties/commitments/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate268.errors = vErrors;
  return errors === 0;
}
validate268.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate267(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate267.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate268(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate268.errors : vErrors.concat(validate268.errors);
    errors = vErrors.length;
  }
  validate267.errors = vErrors;
  return errors === 0;
}
validate267.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__record_exposure_sample_walk_tests__session_id__exposure_samples_post =
  validate272;
const schema156 = { $ref: 'components.json#/$defs/ExposureSampleBody' };
const schema157 = {
  properties: {
    pass_id: { type: 'string', format: 'uuid' },
    fetched_at: { type: 'string', format: 'date-time' },
    displayed_at: { type: 'string', format: 'date-time' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['pass_id', 'fetched_at', 'displayed_at'],
};

function validate273(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate273.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.pass_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'pass_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.fetched_at === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'fetched_at' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.displayed_at === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'displayed_at' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'pass_id' || key0 === 'fetched_at' || key0 === 'displayed_at')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.pass_id !== undefined) {
      let data0 = data.pass_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err4 = {
            instancePath: instancePath + '/pass_id',
            schemaPath: '#/properties/pass_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/pass_id',
          schemaPath: '#/properties/pass_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.fetched_at !== undefined) {
      let data1 = data.fetched_at;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err6 = {
            instancePath: instancePath + '/fetched_at',
            schemaPath: '#/properties/fetched_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/fetched_at',
          schemaPath: '#/properties/fetched_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.displayed_at !== undefined) {
      let data2 = data.displayed_at;
      if (typeof data2 === 'string') {
        if (!formats16.validate(data2)) {
          const err8 = {
            instancePath: instancePath + '/displayed_at',
            schemaPath: '#/properties/displayed_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/displayed_at',
          schemaPath: '#/properties/displayed_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
  } else {
    const err10 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err10];
    } else {
      vErrors.push(err10);
    }
    errors++;
  }
  validate273.errors = vErrors;
  return errors === 0;
}
validate273.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate272(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate272.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate273(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate273.errors : vErrors.concat(validate273.errors);
    errors = vErrors.length;
  }
  validate272.errors = vErrors;
  return errors === 0;
}
validate272.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__record_occlusion_test_walk_tests__session_id__occlusion_tests_post = validate275;
const schema158 = { $ref: 'components.json#/$defs/OcclusionTestBody' };
const schema159 = {
  properties: {
    camera_id: { type: 'string', format: 'uuid' },
    started_at: { type: 'string', format: 'date-time' },
    ended_at: { type: 'string', format: 'date-time' },
    declared_reason_es: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['camera_id', 'started_at', 'ended_at'],
};

function validate276(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate276.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.camera_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'camera_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.started_at === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'started_at' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.ended_at === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'ended_at' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'camera_id' ||
        key0 === 'started_at' ||
        key0 === 'ended_at' ||
        key0 === 'declared_reason_es'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.camera_id !== undefined) {
      let data0 = data.camera_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err4 = {
            instancePath: instancePath + '/camera_id',
            schemaPath: '#/properties/camera_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/camera_id',
          schemaPath: '#/properties/camera_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.started_at !== undefined) {
      let data1 = data.started_at;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err6 = {
            instancePath: instancePath + '/started_at',
            schemaPath: '#/properties/started_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      } else {
        const err7 = {
          instancePath: instancePath + '/started_at',
          schemaPath: '#/properties/started_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.ended_at !== undefined) {
      let data2 = data.ended_at;
      if (typeof data2 === 'string') {
        if (!formats16.validate(data2)) {
          const err8 = {
            instancePath: instancePath + '/ended_at',
            schemaPath: '#/properties/ended_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/ended_at',
          schemaPath: '#/properties/ended_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.declared_reason_es !== undefined) {
      let data3 = data.declared_reason_es;
      const _errs9 = errors;
      let valid1 = false;
      const _errs10 = errors;
      if (typeof data3 !== 'string') {
        const err10 = {
          instancePath: instancePath + '/declared_reason_es',
          schemaPath: '#/properties/declared_reason_es/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid1 = valid1 || _valid0;
      const _errs12 = errors;
      if (data3 !== null) {
        const err11 = {
          instancePath: instancePath + '/declared_reason_es',
          schemaPath: '#/properties/declared_reason_es/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid0 = _errs12 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err12 = {
          instancePath: instancePath + '/declared_reason_es',
          schemaPath: '#/properties/declared_reason_es/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err13 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err13];
    } else {
      vErrors.push(err13);
    }
    errors++;
  }
  validate276.errors = vErrors;
  return errors === 0;
}
validate276.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate275(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate275.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate276(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate276.errors : vErrors.concat(validate276.errors);
    errors = vErrors.length;
  }
  validate275.errors = vErrors;
  return errors === 0;
}
validate275.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__record_pass_walk_tests__session_id__passes_post = validate278;
const schema160 = { $ref: 'components.json#/$defs/PassBody' };
const schema161 = {
  properties: {
    row_id: { type: 'string', format: 'uuid' },
    result: { $ref: 'components.json#/$defs/PassResult' },
    evidence_ref: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['row_id', 'result'],
};
const schema162 = { type: 'string', enum: ['detected', 'missed', 'false_alarm'] };

function validate280(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate280.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'detected' || data === 'missed' || data === 'false_alarm')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema162.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate280.errors = vErrors;
  return errors === 0;
}
validate280.evaluated = { dynamicProps: false, dynamicItems: false };

function validate279(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate279.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.row_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'row_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.result === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'result' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'row_id' || key0 === 'result' || key0 === 'evidence_ref')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.row_id !== undefined) {
      let data0 = data.row_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err3 = {
            instancePath: instancePath + '/row_id',
            schemaPath: '#/properties/row_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/row_id',
          schemaPath: '#/properties/row_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.result !== undefined) {
      if (
        !validate280(data.result, {
          instancePath: instancePath + '/result',
          parentData: data,
          parentDataProperty: 'result',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate280.errors : vErrors.concat(validate280.errors);
        errors = vErrors.length;
      }
    }
    if (data.evidence_ref !== undefined) {
      let data2 = data.evidence_ref;
      const _errs6 = errors;
      let valid1 = false;
      const _errs7 = errors;
      if (typeof data2 === 'string') {
        if (!formats0.test(data2)) {
          const err5 = {
            instancePath: instancePath + '/evidence_ref',
            schemaPath: '#/properties/evidence_ref/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/evidence_ref',
          schemaPath: '#/properties/evidence_ref/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs7 === errors;
      valid1 = valid1 || _valid0;
      const _errs9 = errors;
      if (data2 !== null) {
        const err7 = {
          instancePath: instancePath + '/evidence_ref',
          schemaPath: '#/properties/evidence_ref/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid0 = _errs9 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err8 = {
          instancePath: instancePath + '/evidence_ref',
          schemaPath: '#/properties/evidence_ref/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      } else {
        errors = _errs6;
        if (vErrors !== null) {
          if (_errs6) {
            vErrors.length = _errs6;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate279.errors = vErrors;
  return errors === 0;
}
validate279.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate278(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate278.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate279(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate279.errors : vErrors.concat(validate279.errors);
    errors = vErrors.length;
  }
  validate278.errors = vErrors;
  return errors === 0;
}
validate278.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__record_sample_result_accreditation_sample_items__sample_item_id__result_post =
  validate283;
const schema163 = { $ref: 'components.json#/$defs/SampleResultBody' };
const schema164 = {
  properties: {
    outcome: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    comment: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};

function validate284(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate284.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'outcome' || key0 === 'comment')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.outcome !== undefined) {
      let data0 = data.outcome;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.comment !== undefined) {
      let data1 = data.comment;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/comment',
          schemaPath: '#/properties/comment/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/comment',
          schemaPath: '#/properties/comment/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/comment',
          schemaPath: '#/properties/comment/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate284.errors = vErrors;
  return errors === 0;
}
validate284.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate283(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate283.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate284(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate284.errors : vErrors.concat(validate284.errors);
    errors = vErrors.length;
  }
  validate283.errors = vErrors;
  return errors === 0;
}
validate283.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__reopen_loop_findings__finding_state_id__reopening_post = validate286;
const schema165 = { $ref: 'components.json#/$defs/ReopeningBody' };
const schema166 = {
  properties: {
    reason_text: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    action_type: { type: 'string', maxLength: 64 },
    action_description: { type: 'string', maxLength: 2000, minLength: 20 },
    expected_outcome: {
      anyOf: [{ type: 'string', maxLength: 1000, minLength: 1 }, { type: 'null' }],
    },
    assignee_user_id: { type: 'string', format: 'uuid' },
    deadline_days: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['action_type', 'action_description', 'assignee_user_id'],
};

function validate287(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate287.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.action_type === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'action_type' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.action_description === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'action_description' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.assignee_user_id === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'assignee_user_id' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'reason_text' ||
        key0 === 'action_type' ||
        key0 === 'action_description' ||
        key0 === 'expected_outcome' ||
        key0 === 'assignee_user_id' ||
        key0 === 'deadline_days'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.reason_text !== undefined) {
      let data0 = data.reason_text;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err5 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err6 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.action_type !== undefined) {
      let data1 = data.action_type;
      if (typeof data1 === 'string') {
        if (func1(data1) > 64) {
          const err7 = {
            instancePath: instancePath + '/action_type',
            schemaPath: '#/properties/action_type/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/action_type',
          schemaPath: '#/properties/action_type/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
    }
    if (data.action_description !== undefined) {
      let data2 = data.action_description;
      if (typeof data2 === 'string') {
        if (func1(data2) > 2000) {
          const err9 = {
            instancePath: instancePath + '/action_description',
            schemaPath: '#/properties/action_description/maxLength',
            keyword: 'maxLength',
            params: { limit: 2000 },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
        if (func1(data2) < 20) {
          const err10 = {
            instancePath: instancePath + '/action_description',
            schemaPath: '#/properties/action_description/minLength',
            keyword: 'minLength',
            params: { limit: 20 },
          };
          if (vErrors === null) {
            vErrors = [err10];
          } else {
            vErrors.push(err10);
          }
          errors++;
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/action_description',
          schemaPath: '#/properties/action_description/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
    }
    if (data.expected_outcome !== undefined) {
      let data3 = data.expected_outcome;
      const _errs13 = errors;
      let valid2 = false;
      const _errs14 = errors;
      if (typeof data3 === 'string') {
        if (func1(data3) > 1000) {
          const err12 = {
            instancePath: instancePath + '/expected_outcome',
            schemaPath: '#/properties/expected_outcome/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
        if (func1(data3) < 1) {
          const err13 = {
            instancePath: instancePath + '/expected_outcome',
            schemaPath: '#/properties/expected_outcome/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid2 = valid2 || _valid1;
      const _errs16 = errors;
      if (data3 !== null) {
        const err15 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      }
      var _valid1 = _errs16 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err16 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.assignee_user_id !== undefined) {
      let data4 = data.assignee_user_id;
      if (typeof data4 === 'string') {
        if (!formats0.test(data4)) {
          const err17 = {
            instancePath: instancePath + '/assignee_user_id',
            schemaPath: '#/properties/assignee_user_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err17];
          } else {
            vErrors.push(err17);
          }
          errors++;
        }
      } else {
        const err18 = {
          instancePath: instancePath + '/assignee_user_id',
          schemaPath: '#/properties/assignee_user_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      }
    }
    if (data.deadline_days !== undefined) {
      let data5 = data.deadline_days;
      const _errs21 = errors;
      let valid3 = false;
      const _errs22 = errors;
      if (!(typeof data5 == 'number' && !(data5 % 1) && !isNaN(data5) && isFinite(data5))) {
        const err19 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err19];
        } else {
          vErrors.push(err19);
        }
        errors++;
      }
      var _valid2 = _errs22 === errors;
      valid3 = valid3 || _valid2;
      const _errs24 = errors;
      if (data5 !== null) {
        const err20 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err20];
        } else {
          vErrors.push(err20);
        }
        errors++;
      }
      var _valid2 = _errs24 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err21 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err21];
        } else {
          vErrors.push(err21);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err22 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err22];
    } else {
      vErrors.push(err22);
    }
    errors++;
  }
  validate287.errors = vErrors;
  return errors === 0;
}
validate287.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate286(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate286.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate287(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate287.errors : vErrors.concat(validate287.errors);
    errors = vErrors.length;
  }
  validate286.errors = vErrors;
  return errors === 0;
}
validate286.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__reopen_walk_test_walk_tests__session_id__reopen_post = validate289;
const schema167 = { $ref: 'components.json#/$defs/ReopenBody' };
const schema168 = {
  properties: { reason_es: { type: 'string' } },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es'],
};

function validate290(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate290.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err2 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate290.errors = vErrors;
  return errors === 0;
}
validate290.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate289(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate289.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate290(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate290.errors : vErrors.concat(validate290.errors);
    errors = vErrors.length;
  }
  validate289.errors = vErrors;
  return errors === 0;
}
validate289.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__request_export_accreditation_exports_post = validate292;
const schema169 = { $ref: 'components.json#/$defs/ExportRequestBody' };
const schema170 = {
  properties: {
    plant_id: { type: 'string', format: 'uuid' },
    zone_ids: {
      items: { type: 'string', format: 'uuid' },
      type: 'array',
      maxItems: 80,
      minItems: 1,
    },
    period: { $ref: 'components.json#/$defs/PeriodBody' },
    package_form: { $ref: 'components.json#/$defs/PackageForm' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['plant_id', 'zone_ids', 'period', 'package_form'],
};
const schema171 = {
  properties: {
    from: { type: 'string', format: 'date-time' },
    to: { type: 'string', format: 'date-time' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['from', 'to'],
};

function validate294(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate294.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.from === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'from' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.to === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'to' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'from' || key0 === 'to')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.from !== undefined) {
      let data0 = data.from;
      if (typeof data0 === 'string') {
        if (!formats16.validate(data0)) {
          const err3 = {
            instancePath: instancePath + '/from',
            schemaPath: '#/properties/from/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.to !== undefined) {
      let data1 = data.to;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err5 = {
            instancePath: instancePath + '/to',
            schemaPath: '#/properties/to/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate294.errors = vErrors;
  return errors === 0;
}
validate294.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema172 = { type: 'string', enum: ['with_clips', 'without_clips'] };

function validate296(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate296.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(data === 'with_clips' || data === 'without_clips')) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema172.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate296.errors = vErrors;
  return errors === 0;
}
validate296.evaluated = { dynamicProps: false, dynamicItems: false };

function validate293(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate293.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.plant_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'plant_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.zone_ids === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'zone_ids' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.period === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'period' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.package_form === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'package_form' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'plant_id' ||
        key0 === 'zone_ids' ||
        key0 === 'period' ||
        key0 === 'package_form'
      )) {
        const err4 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data0 = data.plant_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err5 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.zone_ids !== undefined) {
      let data1 = data.zone_ids;
      if (Array.isArray(data1)) {
        if (data1.length > 80) {
          const err7 = {
            instancePath: instancePath + '/zone_ids',
            schemaPath: '#/properties/zone_ids/maxItems',
            keyword: 'maxItems',
            params: { limit: 80 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (data1.length < 1) {
          const err8 = {
            instancePath: instancePath + '/zone_ids',
            schemaPath: '#/properties/zone_ids/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data2 = data1[i0];
          if (typeof data2 === 'string') {
            if (!formats0.test(data2)) {
              const err9 = {
                instancePath: instancePath + '/zone_ids/' + i0,
                schemaPath: '#/properties/zone_ids/items/format',
                keyword: 'format',
                params: { format: 'uuid' },
              };
              if (vErrors === null) {
                vErrors = [err9];
              } else {
                vErrors.push(err9);
              }
              errors++;
            }
          } else {
            const err10 = {
              instancePath: instancePath + '/zone_ids/' + i0,
              schemaPath: '#/properties/zone_ids/items/type',
              keyword: 'type',
              params: { type: 'string' },
            };
            if (vErrors === null) {
              vErrors = [err10];
            } else {
              vErrors.push(err10);
            }
            errors++;
          }
        }
      } else {
        const err11 = {
          instancePath: instancePath + '/zone_ids',
          schemaPath: '#/properties/zone_ids/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
    }
    if (data.period !== undefined) {
      if (
        !validate294(data.period, {
          instancePath: instancePath + '/period',
          parentData: data,
          parentDataProperty: 'period',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate294.errors : vErrors.concat(validate294.errors);
        errors = vErrors.length;
      }
    }
    if (data.package_form !== undefined) {
      if (
        !validate296(data.package_form, {
          instancePath: instancePath + '/package_form',
          parentData: data,
          parentDataProperty: 'package_form',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate296.errors : vErrors.concat(validate296.errors);
        errors = vErrors.length;
      }
    }
  } else {
    const err12 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err12];
    } else {
      vErrors.push(err12);
    }
    errors++;
  }
  validate293.errors = vErrors;
  return errors === 0;
}
validate293.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate292(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate292.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate293(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate293.errors : vErrors.concat(validate293.errors);
    errors = vErrors.length;
  }
  validate292.errors = vErrors;
  return errors === 0;
}
validate292.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__resolve_loop_review_queue__detection_id__resolution_post = validate299;
const schema173 = { $ref: 'components.json#/$defs/ReviewResolutionBody' };
const schema174 = {
  properties: {
    outcome: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    reason_category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    reason_text: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};

function validate300(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate300.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'outcome' || key0 === 'reason_category' || key0 === 'reason_text')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.outcome !== undefined) {
      let data0 = data.outcome;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_category !== undefined) {
      let data1 = data.reason_category;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/reason_category',
          schemaPath: '#/properties/reason_category/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_text !== undefined) {
      let data2 = data.reason_text;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (typeof data2 !== 'string') {
        const err7 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err8 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err9 = {
          instancePath: instancePath + '/reason_text',
          schemaPath: '#/properties/reason_text/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err10 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err10];
    } else {
      vErrors.push(err10);
    }
    errors++;
  }
  validate300.errors = vErrors;
  return errors === 0;
}
validate300.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate299(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate299.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate300(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate300.errors : vErrors.concat(validate300.errors);
    errors = vErrors.length;
  }
  validate299.errors = vErrors;
  return errors === 0;
}
validate299.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__retirement_zones__zone_id__standards__standard_id__retirement_post = validate302;
const schema175 = { $ref: 'components.json#/$defs/RetirementBody' };
const schema176 = {
  properties: {
    reason_es: { type: 'string' },
    effective_from: { type: 'string', format: 'date-time' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es', 'effective_from'],
};

function validate303(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate303.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.effective_from === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'effective_from' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es' || key0 === 'effective_from')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err3 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.effective_from !== undefined) {
      let data1 = data.effective_from;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err4 = {
            instancePath: instancePath + '/effective_from',
            schemaPath: '#/properties/effective_from/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/effective_from',
          schemaPath: '#/properties/effective_from/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate303.errors = vErrors;
  return errors === 0;
}
validate303.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate302(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate302.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate303(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate303.errors : vErrors.concat(validate303.errors);
    errors = vErrors.length;
  }
  validate302.errors = vErrors;
  return errors === 0;
}
validate302.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__review_loop_unclosable__declaration_id__reviews_post = validate305;
const schema177 = { $ref: 'components.json#/$defs/UnclosableReviewBody' };
const schema178 = {
  properties: {
    outcome: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    review_note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    new_review_due_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    action: { anyOf: [{ $ref: 'components.json#/$defs/ReviewActionBody' }, { type: 'null' }] },
    professional_license: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};
const schema179 = {
  properties: {
    action_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    action_description: { anyOf: [{ type: 'string', maxLength: 2000 }, { type: 'null' }] },
    expected_outcome: { anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }] },
    assignee_user_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
    deadline_days: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
};

function validate307(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate307.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'action_type' ||
        key0 === 'action_description' ||
        key0 === 'expected_outcome' ||
        key0 === 'assignee_user_id' ||
        key0 === 'deadline_days'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.action_type !== undefined) {
      let data0 = data.action_type;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/action_type',
          schemaPath: '#/properties/action_type/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/action_type',
          schemaPath: '#/properties/action_type/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/action_type',
          schemaPath: '#/properties/action_type/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.action_description !== undefined) {
      let data1 = data.action_description;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 === 'string') {
        if (func1(data1) > 2000) {
          const err4 = {
            instancePath: instancePath + '/action_description',
            schemaPath: '#/properties/action_description/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 2000 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/action_description',
          schemaPath: '#/properties/action_description/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err6 = {
          instancePath: instancePath + '/action_description',
          schemaPath: '#/properties/action_description/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err7 = {
          instancePath: instancePath + '/action_description',
          schemaPath: '#/properties/action_description/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.expected_outcome !== undefined) {
      let data2 = data.expected_outcome;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (typeof data2 === 'string') {
        if (func1(data2) > 1000) {
          const err8 = {
            instancePath: instancePath + '/expected_outcome',
            schemaPath: '#/properties/expected_outcome/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 1000 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err10 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err11 = {
          instancePath: instancePath + '/expected_outcome',
          schemaPath: '#/properties/expected_outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.assignee_user_id !== undefined) {
      let data3 = data.assignee_user_id;
      const _errs21 = errors;
      let valid4 = false;
      const _errs22 = errors;
      if (typeof data3 === 'string') {
        if (!formats0.test(data3)) {
          const err12 = {
            instancePath: instancePath + '/assignee_user_id',
            schemaPath: '#/properties/assignee_user_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
      } else {
        const err13 = {
          instancePath: instancePath + '/assignee_user_id',
          schemaPath: '#/properties/assignee_user_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid3 = _errs22 === errors;
      valid4 = valid4 || _valid3;
      const _errs24 = errors;
      if (data3 !== null) {
        const err14 = {
          instancePath: instancePath + '/assignee_user_id',
          schemaPath: '#/properties/assignee_user_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err15 = {
          instancePath: instancePath + '/assignee_user_id',
          schemaPath: '#/properties/assignee_user_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.deadline_days !== undefined) {
      let data4 = data.deadline_days;
      const _errs27 = errors;
      let valid5 = false;
      const _errs28 = errors;
      if (!(typeof data4 == 'number' && !(data4 % 1) && !isNaN(data4) && isFinite(data4))) {
        const err16 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err16];
        } else {
          vErrors.push(err16);
        }
        errors++;
      }
      var _valid4 = _errs28 === errors;
      valid5 = valid5 || _valid4;
      const _errs30 = errors;
      if (data4 !== null) {
        const err17 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err17];
        } else {
          vErrors.push(err17);
        }
        errors++;
      }
      var _valid4 = _errs30 === errors;
      valid5 = valid5 || _valid4;
      if (!valid5) {
        const err18 = {
          instancePath: instancePath + '/deadline_days',
          schemaPath: '#/properties/deadline_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err18];
        } else {
          vErrors.push(err18);
        }
        errors++;
      } else {
        errors = _errs27;
        if (vErrors !== null) {
          if (_errs27) {
            vErrors.length = _errs27;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err19 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err19];
    } else {
      vErrors.push(err19);
    }
    errors++;
  }
  validate307.errors = vErrors;
  return errors === 0;
}
validate307.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate306(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate306.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'outcome' ||
        key0 === 'review_note' ||
        key0 === 'new_review_due_at' ||
        key0 === 'action' ||
        key0 === 'professional_license'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.outcome !== undefined) {
      let data0 = data.outcome;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/outcome',
          schemaPath: '#/properties/outcome/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.review_note !== undefined) {
      let data1 = data.review_note;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/review_note',
          schemaPath: '#/properties/review_note/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/review_note',
          schemaPath: '#/properties/review_note/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/review_note',
          schemaPath: '#/properties/review_note/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.new_review_due_at !== undefined) {
      let data2 = data.new_review_due_at;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (typeof data2 !== 'string') {
        const err7 = {
          instancePath: instancePath + '/new_review_due_at',
          schemaPath: '#/properties/new_review_due_at/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err8 = {
          instancePath: instancePath + '/new_review_due_at',
          schemaPath: '#/properties/new_review_due_at/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err9 = {
          instancePath: instancePath + '/new_review_due_at',
          schemaPath: '#/properties/new_review_due_at/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.action !== undefined) {
      let data3 = data.action;
      const _errs21 = errors;
      let valid4 = false;
      const _errs22 = errors;
      if (
        !validate307(data3, {
          instancePath: instancePath + '/action',
          parentData: data,
          parentDataProperty: 'action',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate307.errors : vErrors.concat(validate307.errors);
        errors = vErrors.length;
      }
      var _valid3 = _errs22 === errors;
      valid4 = valid4 || _valid3;
      const _errs23 = errors;
      if (data3 !== null) {
        const err10 = {
          instancePath: instancePath + '/action',
          schemaPath: '#/properties/action/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid3 = _errs23 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err11 = {
          instancePath: instancePath + '/action',
          schemaPath: '#/properties/action/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.professional_license !== undefined) {
      let data4 = data.professional_license;
      const _errs26 = errors;
      let valid5 = false;
      const _errs27 = errors;
      if (typeof data4 !== 'string') {
        const err12 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
      var _valid4 = _errs27 === errors;
      valid5 = valid5 || _valid4;
      const _errs29 = errors;
      if (data4 !== null) {
        const err13 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
      var _valid4 = _errs29 === errors;
      valid5 = valid5 || _valid4;
      if (!valid5) {
        const err14 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      } else {
        errors = _errs26;
        if (vErrors !== null) {
          if (_errs26) {
            vErrors.length = _errs26;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err15 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err15];
    } else {
      vErrors.push(err15);
    }
    errors++;
  }
  validate306.errors = vErrors;
  return errors === 0;
}
validate306.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate305(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate305.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate306(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate306.errors : vErrors.concat(validate306.errors);
    errors = vErrors.length;
  }
  validate305.errors = vErrors;
  return errors === 0;
}
validate305.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__revocation_zones__zone_id__gates__gate__revocation_post = validate310;
const schema180 = { $ref: 'components.json#/$defs/RevocationBody' };
const schema181 = {
  properties: { reason_es: { type: 'string' } },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es'],
};

function validate311(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate311.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err2 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate311.errors = vErrors;
  return errors === 0;
}
validate311.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate310(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate310.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate311(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate311.errors : vErrors.concat(validate311.errors);
    errors = vErrors.length;
  }
  validate310.errors = vErrors;
  return errors === 0;
}
validate310.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__revoke_node_nodes__node_id__revocation_post = validate313;
const schema182 = { $ref: 'components.json#/$defs/NodeReasonBody' };

function validate314(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate314.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err2 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate314.errors = vErrors;
  return errors === 0;
}
validate314.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate313(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate313.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate314(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate314.errors : vErrors.concat(validate314.errors);
    errors = vErrors.length;
  }
  validate313.errors = vErrors;
  return errors === 0;
}
validate313.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__scope_record_zones__zone_id__gates_mounting_scope_record_post = validate316;
const schema184 = { $ref: 'components.json#/$defs/ScopeRecordBody' };
const schema185 = {
  properties: {
    scope_text_es: { type: 'string' },
    cameras: {
      items: { $ref: 'components.json#/$defs/CameraFramingBody' },
      type: 'array',
      maxItems: 8,
      minItems: 1,
    },
    blur_verification: {
      anyOf: [{ $ref: 'components.json#/$defs/BlurVerificationBody' }, { type: 'null' }],
    },
    document_ref: { anyOf: [{ additionalProperties: true, type: 'object' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['scope_text_es', 'cameras'],
};
const schema186 = {
  properties: {
    camera_id: { type: 'string', format: 'uuid' },
    framing_description_es: { type: 'string' },
    reference_marker: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['camera_id', 'framing_description_es', 'reference_marker'],
};

function validate318(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate318.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.camera_id === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'camera_id' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.framing_description_es === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'framing_description_es' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.reference_marker === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reference_marker' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'camera_id' ||
        key0 === 'framing_description_es' ||
        key0 === 'reference_marker'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.camera_id !== undefined) {
      let data0 = data.camera_id;
      if (typeof data0 === 'string') {
        if (!formats0.test(data0)) {
          const err4 = {
            instancePath: instancePath + '/camera_id',
            schemaPath: '#/properties/camera_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/camera_id',
          schemaPath: '#/properties/camera_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.framing_description_es !== undefined) {
      if (typeof data.framing_description_es !== 'string') {
        const err6 = {
          instancePath: instancePath + '/framing_description_es',
          schemaPath: '#/properties/framing_description_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.reference_marker !== undefined) {
      if (typeof data.reference_marker !== 'boolean') {
        const err7 = {
          instancePath: instancePath + '/reference_marker',
          schemaPath: '#/properties/reference_marker/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate318.errors = vErrors;
  return errors === 0;
}
validate318.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

const schema187 = {
  properties: {
    declared: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
    capture_document_ref: {
      anyOf: [{ additionalProperties: true, type: 'object' }, { type: 'null' }],
    },
  },
  additionalProperties: false,
  type: 'object',
};

function validate320(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate320.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(key0 === 'declared' || key0 === 'capture_document_ref')) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.declared !== undefined) {
      let data0 = data.declared;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'boolean') {
        const err1 = {
          instancePath: instancePath + '/declared',
          schemaPath: '#/properties/declared/anyOf/0/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/declared',
          schemaPath: '#/properties/declared/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/declared',
          schemaPath: '#/properties/declared/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.capture_document_ref !== undefined) {
      let data1 = data.capture_document_ref;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (data1 && typeof data1 == 'object' && !Array.isArray(data1)) {
      } else {
        const err4 = {
          instancePath: instancePath + '/capture_document_ref',
          schemaPath: '#/properties/capture_document_ref/anyOf/0/type',
          keyword: 'type',
          params: { type: 'object' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs13 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/capture_document_ref',
          schemaPath: '#/properties/capture_document_ref/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs13 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/capture_document_ref',
          schemaPath: '#/properties/capture_document_ref/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate320.errors = vErrors;
  return errors === 0;
}
validate320.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate317(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate317.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.scope_text_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'scope_text_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.cameras === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'cameras' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'scope_text_es' ||
        key0 === 'cameras' ||
        key0 === 'blur_verification' ||
        key0 === 'document_ref'
      )) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.scope_text_es !== undefined) {
      if (typeof data.scope_text_es !== 'string') {
        const err3 = {
          instancePath: instancePath + '/scope_text_es',
          schemaPath: '#/properties/scope_text_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.cameras !== undefined) {
      let data1 = data.cameras;
      if (Array.isArray(data1)) {
        if (data1.length > 8) {
          const err4 = {
            instancePath: instancePath + '/cameras',
            schemaPath: '#/properties/cameras/maxItems',
            keyword: 'maxItems',
            params: { limit: 8 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        if (data1.length < 1) {
          const err5 = {
            instancePath: instancePath + '/cameras',
            schemaPath: '#/properties/cameras/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate318(data1[i0], {
              instancePath: instancePath + '/cameras/' + i0,
              parentData: data1,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate318.errors : vErrors.concat(validate318.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/cameras',
          schemaPath: '#/properties/cameras/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.blur_verification !== undefined) {
      let data3 = data.blur_verification;
      const _errs8 = errors;
      let valid3 = false;
      const _errs9 = errors;
      if (
        !validate320(data3, {
          instancePath: instancePath + '/blur_verification',
          parentData: data,
          parentDataProperty: 'blur_verification',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate320.errors : vErrors.concat(validate320.errors);
        errors = vErrors.length;
      }
      var _valid0 = _errs9 === errors;
      valid3 = valid3 || _valid0;
      const _errs10 = errors;
      if (data3 !== null) {
        const err7 = {
          instancePath: instancePath + '/blur_verification',
          schemaPath: '#/properties/blur_verification/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid0 = _errs10 === errors;
      valid3 = valid3 || _valid0;
      if (!valid3) {
        const err8 = {
          instancePath: instancePath + '/blur_verification',
          schemaPath: '#/properties/blur_verification/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      } else {
        errors = _errs8;
        if (vErrors !== null) {
          if (_errs8) {
            vErrors.length = _errs8;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.document_ref !== undefined) {
      let data4 = data.document_ref;
      const _errs13 = errors;
      let valid4 = false;
      const _errs14 = errors;
      if (data4 && typeof data4 == 'object' && !Array.isArray(data4)) {
      } else {
        const err9 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/anyOf/0/type',
          keyword: 'type',
          params: { type: 'object' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs14 === errors;
      valid4 = valid4 || _valid1;
      const _errs17 = errors;
      if (data4 !== null) {
        const err10 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid1 = _errs17 === errors;
      valid4 = valid4 || _valid1;
      if (!valid4) {
        const err11 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      } else {
        errors = _errs13;
        if (vErrors !== null) {
          if (_errs13) {
            vErrors.length = _errs13;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err12 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err12];
    } else {
      vErrors.push(err12);
    }
    errors++;
  }
  validate317.errors = vErrors;
  return errors === 0;
}
validate317.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate316(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate316.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate317(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate317.errors : vErrors.concat(validate317.errors);
    errors = vErrors.length;
  }
  validate316.errors = vErrors;
  return errors === 0;
}
validate316.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__second_factor_auth_second_factor_post = validate323;
const schema188 = { $ref: 'components.json#/$defs/SecondFactorRequest' };
const schema189 = {
  properties: { code: { type: 'string', maxLength: 64, minLength: 1 } },
  additionalProperties: false,
  type: 'object',
  required: ['code'],
};

function validate324(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate324.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.code === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'code' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'code')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.code !== undefined) {
      let data0 = data.code;
      if (typeof data0 === 'string') {
        if (func1(data0) > 64) {
          const err2 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err3 = {
            instancePath: instancePath + '/code',
            schemaPath: '#/properties/code/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/code',
          schemaPath: '#/properties/code/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate324.errors = vErrors;
  return errors === 0;
}
validate324.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate323(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate323.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate324(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate324.errors : vErrors.concat(validate324.errors);
    errors = vErrors.length;
  }
  validate323.errors = vErrors;
  return errors === 0;
}
validate323.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__sign_policy_plants__plant_id__policy_post = validate326;
const schema190 = { $ref: 'components.json#/$defs/PlantPolicyBody' };
const schema191 = {
  properties: {
    version: { type: 'integer' },
    signed_at: { type: 'string', format: 'date-time' },
    signed_by_display_name: { type: 'string' },
    legal_opinion_reference: { type: 'string' },
    criteria_summary_es: { type: 'string' },
    document_ref: { additionalProperties: true, type: 'object' },
  },
  additionalProperties: false,
  type: 'object',
  required: [
    'version',
    'signed_at',
    'signed_by_display_name',
    'legal_opinion_reference',
    'criteria_summary_es',
    'document_ref',
  ],
};

function validate327(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate327.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.version === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'version' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.signed_at === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'signed_at' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.signed_by_display_name === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'signed_by_display_name' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    if (data.legal_opinion_reference === undefined) {
      const err3 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'legal_opinion_reference' },
      };
      if (vErrors === null) {
        vErrors = [err3];
      } else {
        vErrors.push(err3);
      }
      errors++;
    }
    if (data.criteria_summary_es === undefined) {
      const err4 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'criteria_summary_es' },
      };
      if (vErrors === null) {
        vErrors = [err4];
      } else {
        vErrors.push(err4);
      }
      errors++;
    }
    if (data.document_ref === undefined) {
      const err5 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'document_ref' },
      };
      if (vErrors === null) {
        vErrors = [err5];
      } else {
        vErrors.push(err5);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'version' ||
        key0 === 'signed_at' ||
        key0 === 'signed_by_display_name' ||
        key0 === 'legal_opinion_reference' ||
        key0 === 'criteria_summary_es' ||
        key0 === 'document_ref'
      )) {
        const err6 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.version !== undefined) {
      let data0 = data.version;
      if (!(typeof data0 == 'number' && !(data0 % 1) && !isNaN(data0) && isFinite(data0))) {
        const err7 = {
          instancePath: instancePath + '/version',
          schemaPath: '#/properties/version/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
    if (data.signed_at !== undefined) {
      let data1 = data.signed_at;
      if (typeof data1 === 'string') {
        if (!formats16.validate(data1)) {
          const err8 = {
            instancePath: instancePath + '/signed_at',
            schemaPath: '#/properties/signed_at/format',
            keyword: 'format',
            params: { format: 'date-time' },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/signed_at',
          schemaPath: '#/properties/signed_at/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.signed_by_display_name !== undefined) {
      if (typeof data.signed_by_display_name !== 'string') {
        const err10 = {
          instancePath: instancePath + '/signed_by_display_name',
          schemaPath: '#/properties/signed_by_display_name/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
    if (data.legal_opinion_reference !== undefined) {
      if (typeof data.legal_opinion_reference !== 'string') {
        const err11 = {
          instancePath: instancePath + '/legal_opinion_reference',
          schemaPath: '#/properties/legal_opinion_reference/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
    }
    if (data.criteria_summary_es !== undefined) {
      if (typeof data.criteria_summary_es !== 'string') {
        const err12 = {
          instancePath: instancePath + '/criteria_summary_es',
          schemaPath: '#/properties/criteria_summary_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      }
    }
    if (data.document_ref !== undefined) {
      let data5 = data.document_ref;
      if (data5 && typeof data5 == 'object' && !Array.isArray(data5)) {
      } else {
        const err13 = {
          instancePath: instancePath + '/document_ref',
          schemaPath: '#/properties/document_ref/type',
          keyword: 'type',
          params: { type: 'object' },
        };
        if (vErrors === null) {
          vErrors = [err13];
        } else {
          vErrors.push(err13);
        }
        errors++;
      }
    }
  } else {
    const err14 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err14];
    } else {
      vErrors.push(err14);
    }
    errors++;
  }
  validate327.errors = vErrors;
  return errors === 0;
}
validate327.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate326(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate326.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate327(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate327.errors : vErrors.concat(validate327.errors);
    errors = vErrors.length;
  }
  validate326.errors = vErrors;
  return errors === 0;
}
validate326.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__signals_zones__zone_id__signals_put = validate329;
const schema192 = { $ref: 'components.json#/$defs/SignalsBody' };
const schema193 = {
  properties: {
    signals: { items: { additionalProperties: true, type: 'object' }, type: 'array', maxItems: 32 },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['signals', 'reason_es'],
};

function validate330(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate330.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.signals === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'signals' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'signals' || key0 === 'reason_es')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.signals !== undefined) {
      let data0 = data.signals;
      if (Array.isArray(data0)) {
        if (data0.length > 32) {
          const err3 = {
            instancePath: instancePath + '/signals',
            schemaPath: '#/properties/signals/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data0.length;
        for (let i0 = 0; i0 < len0; i0++) {
          let data1 = data0[i0];
          if (data1 && typeof data1 == 'object' && !Array.isArray(data1)) {
          } else {
            const err4 = {
              instancePath: instancePath + '/signals/' + i0,
              schemaPath: '#/properties/signals/items/type',
              keyword: 'type',
              params: { type: 'object' },
            };
            if (vErrors === null) {
              vErrors = [err4];
            } else {
              vErrors.push(err4);
            }
            errors++;
          }
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/signals',
          schemaPath: '#/properties/signals/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err6 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate330.errors = vErrors;
  return errors === 0;
}
validate330.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate329(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate329.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate330(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate330.errors : vErrors.concat(validate330.errors);
    errors = vErrors.length;
  }
  validate329.errors = vErrors;
  return errors === 0;
}
validate329.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__signature_loop_root_cause_analyses__analysis_id__signature_post = validate332;
const schema194 = { $ref: 'components.json#/$defs/AnalysisSignatureBody' };
const schema195 = {
  properties: {
    professional_license: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    root_cause_category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    root_cause_statement: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    conclusion: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    cause_effect_branches: {
      items: { $ref: 'components.json#/$defs/BranchBody' },
      type: 'array',
      maxItems: 6,
    },
  },
  additionalProperties: false,
  type: 'object',
};
const schema196 = {
  properties: {
    branch_category: { type: 'string' },
    factors: {
      items: { $ref: 'components.json#/$defs/FactorBody' },
      type: 'array',
      maxItems: 16,
      minItems: 1,
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['branch_category', 'factors'],
};
const schema197 = {
  properties: {
    text: { type: 'string' },
    evidence_refs: {
      items: { $ref: 'components.json#/$defs/EvidenceRefBody' },
      type: 'array',
      maxItems: 32,
    },
  },
  additionalProperties: false,
  type: 'object',
  required: ['text'],
};

function validate335(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate335.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.text === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'text' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'text' || key0 === 'evidence_refs')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.text !== undefined) {
      if (typeof data.text !== 'string') {
        const err2 = {
          instancePath: instancePath + '/text',
          schemaPath: '#/properties/text/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.evidence_refs !== undefined) {
      let data1 = data.evidence_refs;
      if (Array.isArray(data1)) {
        if (data1.length > 32) {
          const err3 = {
            instancePath: instancePath + '/evidence_refs',
            schemaPath: '#/properties/evidence_refs/maxItems',
            keyword: 'maxItems',
            params: { limit: 32 },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate260(data1[i0], {
              instancePath: instancePath + '/evidence_refs/' + i0,
              parentData: data1,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate260.errors : vErrors.concat(validate260.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/evidence_refs',
          schemaPath: '#/properties/evidence_refs/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate335.errors = vErrors;
  return errors === 0;
}
validate335.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate334(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate334.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.branch_category === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'branch_category' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.factors === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'factors' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'branch_category' || key0 === 'factors')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.branch_category !== undefined) {
      if (typeof data.branch_category !== 'string') {
        const err3 = {
          instancePath: instancePath + '/branch_category',
          schemaPath: '#/properties/branch_category/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.factors !== undefined) {
      let data1 = data.factors;
      if (Array.isArray(data1)) {
        if (data1.length > 16) {
          const err4 = {
            instancePath: instancePath + '/factors',
            schemaPath: '#/properties/factors/maxItems',
            keyword: 'maxItems',
            params: { limit: 16 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        if (data1.length < 1) {
          const err5 = {
            instancePath: instancePath + '/factors',
            schemaPath: '#/properties/factors/minItems',
            keyword: 'minItems',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
        const len0 = data1.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate335(data1[i0], {
              instancePath: instancePath + '/factors/' + i0,
              parentData: data1,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate335.errors : vErrors.concat(validate335.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/factors',
          schemaPath: '#/properties/factors/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate334.errors = vErrors;
  return errors === 0;
}
validate334.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate333(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate333.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'professional_license' ||
        key0 === 'root_cause_category' ||
        key0 === 'root_cause_statement' ||
        key0 === 'conclusion' ||
        key0 === 'cause_effect_branches'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.professional_license !== undefined) {
      let data0 = data.professional_license;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 !== 'string') {
        const err1 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.root_cause_category !== undefined) {
      let data1 = data.root_cause_category;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 !== 'string') {
        const err4 = {
          instancePath: instancePath + '/root_cause_category',
          schemaPath: '#/properties/root_cause_category/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err5 = {
          instancePath: instancePath + '/root_cause_category',
          schemaPath: '#/properties/root_cause_category/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err6 = {
          instancePath: instancePath + '/root_cause_category',
          schemaPath: '#/properties/root_cause_category/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.root_cause_statement !== undefined) {
      let data2 = data.root_cause_statement;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (typeof data2 !== 'string') {
        const err7 = {
          instancePath: instancePath + '/root_cause_statement',
          schemaPath: '#/properties/root_cause_statement/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err8 = {
          instancePath: instancePath + '/root_cause_statement',
          schemaPath: '#/properties/root_cause_statement/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err9 = {
          instancePath: instancePath + '/root_cause_statement',
          schemaPath: '#/properties/root_cause_statement/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.conclusion !== undefined) {
      let data3 = data.conclusion;
      const _errs21 = errors;
      let valid4 = false;
      const _errs22 = errors;
      if (typeof data3 !== 'string') {
        const err10 = {
          instancePath: instancePath + '/conclusion',
          schemaPath: '#/properties/conclusion/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
      var _valid3 = _errs22 === errors;
      valid4 = valid4 || _valid3;
      const _errs24 = errors;
      if (data3 !== null) {
        const err11 = {
          instancePath: instancePath + '/conclusion',
          schemaPath: '#/properties/conclusion/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      var _valid3 = _errs24 === errors;
      valid4 = valid4 || _valid3;
      if (!valid4) {
        const err12 = {
          instancePath: instancePath + '/conclusion',
          schemaPath: '#/properties/conclusion/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err12];
        } else {
          vErrors.push(err12);
        }
        errors++;
      } else {
        errors = _errs21;
        if (vErrors !== null) {
          if (_errs21) {
            vErrors.length = _errs21;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.cause_effect_branches !== undefined) {
      let data4 = data.cause_effect_branches;
      if (Array.isArray(data4)) {
        if (data4.length > 6) {
          const err13 = {
            instancePath: instancePath + '/cause_effect_branches',
            schemaPath: '#/properties/cause_effect_branches/maxItems',
            keyword: 'maxItems',
            params: { limit: 6 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
        const len0 = data4.length;
        for (let i0 = 0; i0 < len0; i0++) {
          if (
            !validate334(data4[i0], {
              instancePath: instancePath + '/cause_effect_branches/' + i0,
              parentData: data4,
              parentDataProperty: i0,
              rootData,
              dynamicAnchors,
            })
          ) {
            vErrors = vErrors === null ? validate334.errors : vErrors.concat(validate334.errors);
            errors = vErrors.length;
          }
        }
      } else {
        const err14 = {
          instancePath: instancePath + '/cause_effect_branches',
          schemaPath: '#/properties/cause_effect_branches/type',
          keyword: 'type',
          params: { type: 'array' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
    }
  } else {
    const err15 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err15];
    } else {
      vErrors.push(err15);
    }
    errors++;
  }
  validate333.errors = vErrors;
  return errors === 0;
}
validate333.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate332(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate332.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate333(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate333.errors : vErrors.concat(validate333.errors);
    errors = vErrors.length;
  }
  validate332.errors = vErrors;
  return errors === 0;
}
validate332.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__single_occupancy_zones__zone_id__catalog_single_occupancy_put = validate340;
const schema198 = { $ref: 'components.json#/$defs/SingleOccupancyBody' };
const schema199 = {
  properties: {
    single_occupancy: { type: 'boolean' },
    aggregation_window_minutes: { type: 'integer', maximum: 480, minimum: 15 },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['single_occupancy', 'reason_es'],
};

function validate341(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate341.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.single_occupancy === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'single_occupancy' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'single_occupancy' ||
        key0 === 'aggregation_window_minutes' ||
        key0 === 'reason_es'
      )) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.single_occupancy !== undefined) {
      if (typeof data.single_occupancy !== 'boolean') {
        const err3 = {
          instancePath: instancePath + '/single_occupancy',
          schemaPath: '#/properties/single_occupancy/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.aggregation_window_minutes !== undefined) {
      let data1 = data.aggregation_window_minutes;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err4 = {
          instancePath: instancePath + '/aggregation_window_minutes',
          schemaPath: '#/properties/aggregation_window_minutes/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 480 || isNaN(data1)) {
          const err5 = {
            instancePath: instancePath + '/aggregation_window_minutes',
            schemaPath: '#/properties/aggregation_window_minutes/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 480 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
        if (data1 < 15 || isNaN(data1)) {
          const err6 = {
            instancePath: instancePath + '/aggregation_window_minutes',
            schemaPath: '#/properties/aggregation_window_minutes/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 15 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err7 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate341.errors = vErrors;
  return errors === 0;
}
validate341.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate340(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate340.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate341(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate341.errors : vErrors.concat(validate341.errors);
    errors = vErrors.length;
  }
  validate340.errors = vErrors;
  return errors === 0;
}
validate340.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__start_step_walk_tests__session_id__steps_post = validate343;
const schema200 = { $ref: 'components.json#/$defs/StepBody' };
const schema201 = {
  properties: {
    step_kind: { $ref: 'components.json#/$defs/StepKind' },
    responsible_user_id: { type: 'string', format: 'uuid' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['step_kind', 'responsible_user_id'],
};
const schema202 = {
  type: 'string',
  enum: [
    'physical_setup',
    'signal_mapping',
    'framing',
    'walk_test_passes',
    'occlusion_test',
    'latency_measurement',
    'review_and_signatures',
    'other',
  ],
};

function validate345(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate345.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (typeof data !== 'string') {
    const err0 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'string' },
    };
    if (vErrors === null) {
      vErrors = [err0];
    } else {
      vErrors.push(err0);
    }
    errors++;
  }
  if (!(
    data === 'physical_setup' ||
    data === 'signal_mapping' ||
    data === 'framing' ||
    data === 'walk_test_passes' ||
    data === 'occlusion_test' ||
    data === 'latency_measurement' ||
    data === 'review_and_signatures' ||
    data === 'other'
  )) {
    const err1 = {
      instancePath,
      schemaPath: '#/enum',
      keyword: 'enum',
      params: { allowedValues: schema202.enum },
    };
    if (vErrors === null) {
      vErrors = [err1];
    } else {
      vErrors.push(err1);
    }
    errors++;
  }
  validate345.errors = vErrors;
  return errors === 0;
}
validate345.evaluated = { dynamicProps: false, dynamicItems: false };

function validate344(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate344.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.step_kind === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'step_kind' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.responsible_user_id === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'responsible_user_id' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'step_kind' || key0 === 'responsible_user_id')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.step_kind !== undefined) {
      if (
        !validate345(data.step_kind, {
          instancePath: instancePath + '/step_kind',
          parentData: data,
          parentDataProperty: 'step_kind',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate345.errors : vErrors.concat(validate345.errors);
        errors = vErrors.length;
      }
    }
    if (data.responsible_user_id !== undefined) {
      let data1 = data.responsible_user_id;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err3 = {
            instancePath: instancePath + '/responsible_user_id',
            schemaPath: '#/properties/responsible_user_id/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err3];
          } else {
            vErrors.push(err3);
          }
          errors++;
        }
      } else {
        const err4 = {
          instancePath: instancePath + '/responsible_user_id',
          schemaPath: '#/properties/responsible_user_id/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate344.errors = vErrors;
  return errors === 0;
}
validate344.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate343(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate343.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate344(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate344.errors : vErrors.concat(validate344.errors);
    errors = vErrors.length;
  }
  validate343.errors = vErrors;
  return errors === 0;
}
validate343.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__status_at_zones__zone_id__coverage_at_get = validate348;
const schema203 = {
  type: 'object',
  properties: { instant: { type: 'string' } },
  required: ['instant'],
  additionalProperties: false,
};

function validate348(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate348.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.instant === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'instant' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'instant')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.instant !== undefined) {
      if (typeof data.instant !== 'string') {
        const err2 = {
          instancePath: instancePath + '/instant',
          schemaPath: '#/properties/instant/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate348.errors = vErrors;
  return errors === 0;
}
validate348.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__thresholds_zones__zone_id__thresholds_put = validate349;
const schema204 = { $ref: 'components.json#/$defs/ThresholdsBody' };
const schema205 = {
  properties: {
    review: { type: 'number', exclusiveMaximum: 1, exclusiveMinimum: 0 },
    publication: { type: 'number', maximum: 1, exclusiveMinimum: 0 },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['review', 'publication', 'reason_es'],
};

function validate350(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate350.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.review === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'review' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.publication === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'publication' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.reason_es === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'review' || key0 === 'publication' || key0 === 'reason_es')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.review !== undefined) {
      let data0 = data.review;
      if (typeof data0 == 'number' && isFinite(data0)) {
        if (data0 >= 1 || isNaN(data0)) {
          const err4 = {
            instancePath: instancePath + '/review',
            schemaPath: '#/properties/review/exclusiveMaximum',
            keyword: 'exclusiveMaximum',
            params: { comparison: '<', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        if (data0 <= 0 || isNaN(data0)) {
          const err5 = {
            instancePath: instancePath + '/review',
            schemaPath: '#/properties/review/exclusiveMinimum',
            keyword: 'exclusiveMinimum',
            params: { comparison: '>', limit: 0 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/review',
          schemaPath: '#/properties/review/type',
          keyword: 'type',
          params: { type: 'number' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.publication !== undefined) {
      let data1 = data.publication;
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 1 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/publication',
            schemaPath: '#/properties/publication/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (data1 <= 0 || isNaN(data1)) {
          const err8 = {
            instancePath: instancePath + '/publication',
            schemaPath: '#/properties/publication/exclusiveMinimum',
            keyword: 'exclusiveMinimum',
            params: { comparison: '>', limit: 0 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      } else {
        const err9 = {
          instancePath: instancePath + '/publication',
          schemaPath: '#/properties/publication/type',
          keyword: 'type',
          params: { type: 'number' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err10 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
  } else {
    const err11 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err11];
    } else {
      vErrors.push(err11);
    }
    errors++;
  }
  validate350.errors = vErrors;
  return errors === 0;
}
validate350.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate349(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate349.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate350(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate350.errors : vErrors.concat(validate350.errors);
    errors = vErrors.length;
  }
  validate349.errors = vErrors;
  return errors === 0;
}
validate349.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const query__timeline_zones__zone_id__coverage_get = validate352;
const schema206 = {
  type: 'object',
  properties: { from: { type: 'string' }, to: { type: 'string' } },
  required: ['from', 'to'],
  additionalProperties: false,
};

function validate352(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate352.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.from === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'from' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.to === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'to' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'from' || key0 === 'to')) {
        const err2 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.from !== undefined) {
      if (typeof data.from !== 'string') {
        const err3 = {
          instancePath: instancePath + '/from',
          schemaPath: '#/properties/from/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.to !== undefined) {
      if (typeof data.to !== 'string') {
        const err4 = {
          instancePath: instancePath + '/to',
          schemaPath: '#/properties/to/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
  } else {
    const err5 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err5];
    } else {
      vErrors.push(err5);
    }
    errors++;
  }
  validate352.errors = vErrors;
  return errors === 0;
}
validate352.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__unassign_zone_nodes__node_id__zones__zone_id__unassignment_post = validate353;
const schema207 = { $ref: 'components.json#/$defs/NodeReasonBody' };

function validate354(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate354.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err2 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
  } else {
    const err3 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err3];
    } else {
      vErrors.push(err3);
    }
    errors++;
  }
  validate354.errors = vErrors;
  return errors === 0;
}
validate354.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate353(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate353.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate354(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate354.errors : vErrors.concat(validate354.errors);
    errors = vErrors.length;
  }
  validate353.errors = vErrors;
  return errors === 0;
}
validate353.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__update_commitment_accreditation_commitments__commitment_id__patch = validate356;
const schema209 = { $ref: 'components.json#/$defs/CommitmentUpdateBody' };
const schema210 = {
  properties: {
    state: { type: 'string' },
    note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['state'],
};

function validate357(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate357.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.state === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'state' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'state' || key0 === 'note')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.state !== undefined) {
      if (typeof data.state !== 'string') {
        const err2 = {
          instancePath: instancePath + '/state',
          schemaPath: '#/properties/state/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
    }
    if (data.note !== undefined) {
      let data1 = data.note;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (typeof data1 !== 'string') {
        const err3 = {
          instancePath: instancePath + '/note',
          schemaPath: '#/properties/note/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data1 !== null) {
        const err4 = {
          instancePath: instancePath + '/note',
          schemaPath: '#/properties/note/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/note',
          schemaPath: '#/properties/note/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err6 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err6];
    } else {
      vErrors.push(err6);
    }
    errors++;
  }
  validate357.errors = vErrors;
  return errors === 0;
}
validate357.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate356(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate356.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate357(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate357.errors : vErrors.concat(validate357.errors);
    errors = vErrors.length;
  }
  validate356.errors = vErrors;
  return errors === 0;
}
validate356.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__update_preferences_notifications_preferences_put = validate359;
const schema211 = { $ref: 'components.json#/$defs/NotificationPreferenceUpdateIn' };
const schema212 = {
  properties: {
    email_enabled_normal: { type: 'boolean' },
    email_enabled_digest: { type: 'boolean' },
    weekly_digest_weekday: { type: 'integer', maximum: 7, minimum: 1 },
  },
  additionalProperties: false,
  type: 'object',
  required: ['email_enabled_normal', 'email_enabled_digest', 'weekly_digest_weekday'],
};

function validate360(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate360.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.email_enabled_normal === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'email_enabled_normal' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.email_enabled_digest === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'email_enabled_digest' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.weekly_digest_weekday === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'weekly_digest_weekday' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(
        key0 === 'email_enabled_normal' ||
        key0 === 'email_enabled_digest' ||
        key0 === 'weekly_digest_weekday'
      )) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.email_enabled_normal !== undefined) {
      if (typeof data.email_enabled_normal !== 'boolean') {
        const err4 = {
          instancePath: instancePath + '/email_enabled_normal',
          schemaPath: '#/properties/email_enabled_normal/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
    }
    if (data.email_enabled_digest !== undefined) {
      if (typeof data.email_enabled_digest !== 'boolean') {
        const err5 = {
          instancePath: instancePath + '/email_enabled_digest',
          schemaPath: '#/properties/email_enabled_digest/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
    }
    if (data.weekly_digest_weekday !== undefined) {
      let data2 = data.weekly_digest_weekday;
      if (!(typeof data2 == 'number' && !(data2 % 1) && !isNaN(data2) && isFinite(data2))) {
        const err6 = {
          instancePath: instancePath + '/weekly_digest_weekday',
          schemaPath: '#/properties/weekly_digest_weekday/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      if (typeof data2 == 'number' && isFinite(data2)) {
        if (data2 > 7 || isNaN(data2)) {
          const err7 = {
            instancePath: instancePath + '/weekly_digest_weekday',
            schemaPath: '#/properties/weekly_digest_weekday/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 7 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (data2 < 1 || isNaN(data2)) {
          const err8 = {
            instancePath: instancePath + '/weekly_digest_weekday',
            schemaPath: '#/properties/weekly_digest_weekday/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      }
    }
  } else {
    const err9 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err9];
    } else {
      vErrors.push(err9);
    }
    errors++;
  }
  validate360.errors = vErrors;
  return errors === 0;
}
validate360.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate359(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate359.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate360(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate360.errors : vErrors.concat(validate360.errors);
    errors = vErrors.length;
  }
  validate359.errors = vErrors;
  return errors === 0;
}
validate359.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__update_profile_users__user_id__patch = validate362;
const schema213 = { $ref: 'components.json#/$defs/ProfileBody' };
const schema214 = {
  properties: {
    display_name: { anyOf: [{ type: 'string', maxLength: 120, minLength: 1 }, { type: 'null' }] },
    professional_license: {
      anyOf: [{ type: 'string', maxLength: 64, minLength: 1 }, { type: 'null' }],
    },
    clear_professional_license: { type: 'boolean' },
  },
  additionalProperties: false,
  type: 'object',
};

function validate363(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate363.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'display_name' ||
        key0 === 'professional_license' ||
        key0 === 'clear_professional_license'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.display_name !== undefined) {
      let data0 = data.display_name;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 120) {
          const err1 = {
            instancePath: instancePath + '/display_name',
            schemaPath: '#/properties/display_name/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 120 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err2 = {
            instancePath: instancePath + '/display_name',
            schemaPath: '#/properties/display_name/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/display_name',
          schemaPath: '#/properties/display_name/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err4 = {
          instancePath: instancePath + '/display_name',
          schemaPath: '#/properties/display_name/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/display_name',
          schemaPath: '#/properties/display_name/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.professional_license !== undefined) {
      let data1 = data.professional_license;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (typeof data1 === 'string') {
        if (func1(data1) > 64) {
          const err6 = {
            instancePath: instancePath + '/professional_license',
            schemaPath: '#/properties/professional_license/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 64 },
          };
          if (vErrors === null) {
            vErrors = [err6];
          } else {
            vErrors.push(err6);
          }
          errors++;
        }
        if (func1(data1) < 1) {
          const err7 = {
            instancePath: instancePath + '/professional_license',
            schemaPath: '#/properties/professional_license/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
      } else {
        const err8 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err8];
        } else {
          vErrors.push(err8);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err9 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err10 = {
          instancePath: instancePath + '/professional_license',
          schemaPath: '#/properties/professional_license/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.clear_professional_license !== undefined) {
      if (typeof data.clear_professional_license !== 'boolean') {
        const err11 = {
          instancePath: instancePath + '/clear_professional_license',
          schemaPath: '#/properties/clear_professional_license/type',
          keyword: 'type',
          params: { type: 'boolean' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
    }
  } else {
    const err12 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err12];
    } else {
      vErrors.push(err12);
    }
    errors++;
  }
  validate363.errors = vErrors;
  return errors === 0;
}
validate363.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate362(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate362.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate363(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate363.errors : vErrors.concat(validate363.errors);
    errors = vErrors.length;
  }
  validate362.errors = vErrors;
  return errors === 0;
}
validate362.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__update_settings_organization_settings_patch = validate365;
const schema215 = { $ref: 'components.json#/$defs/SettingsBody' };
const schema216 = {
  properties: {
    name: { anyOf: [{ type: 'string', maxLength: 120, minLength: 1 }, { type: 'null' }] },
    concession_max_days: {
      anyOf: [{ type: 'integer', maximum: 90, minimum: 1 }, { type: 'null' }],
    },
    concession_default_days: {
      anyOf: [{ type: 'integer', maximum: 90, minimum: 1 }, { type: 'null' }],
    },
  },
  additionalProperties: false,
  type: 'object',
};

function validate366(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate366.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    for (const key0 in data) {
      if (!(
        key0 === 'name' ||
        key0 === 'concession_max_days' ||
        key0 === 'concession_default_days'
      )) {
        const err0 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err0];
        } else {
          vErrors.push(err0);
        }
        errors++;
      }
    }
    if (data.name !== undefined) {
      let data0 = data.name;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (typeof data0 === 'string') {
        if (func1(data0) > 120) {
          const err1 = {
            instancePath: instancePath + '/name',
            schemaPath: '#/properties/name/anyOf/0/maxLength',
            keyword: 'maxLength',
            params: { limit: 120 },
          };
          if (vErrors === null) {
            vErrors = [err1];
          } else {
            vErrors.push(err1);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err2 = {
            instancePath: instancePath + '/name',
            schemaPath: '#/properties/name/anyOf/0/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err2];
          } else {
            vErrors.push(err2);
          }
          errors++;
        }
      } else {
        const err3 = {
          instancePath: instancePath + '/name',
          schemaPath: '#/properties/name/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs6 = errors;
      if (data0 !== null) {
        const err4 = {
          instancePath: instancePath + '/name',
          schemaPath: '#/properties/name/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err5 = {
          instancePath: instancePath + '/name',
          schemaPath: '#/properties/name/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.concession_max_days !== undefined) {
      let data1 = data.concession_max_days;
      const _errs9 = errors;
      let valid2 = false;
      const _errs10 = errors;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err6 = {
          instancePath: instancePath + '/concession_max_days',
          schemaPath: '#/properties/concession_max_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 > 90 || isNaN(data1)) {
          const err7 = {
            instancePath: instancePath + '/concession_max_days',
            schemaPath: '#/properties/concession_max_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 90 },
          };
          if (vErrors === null) {
            vErrors = [err7];
          } else {
            vErrors.push(err7);
          }
          errors++;
        }
        if (data1 < 1 || isNaN(data1)) {
          const err8 = {
            instancePath: instancePath + '/concession_max_days',
            schemaPath: '#/properties/concession_max_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      const _errs12 = errors;
      if (data1 !== null) {
        const err9 = {
          instancePath: instancePath + '/concession_max_days',
          schemaPath: '#/properties/concession_max_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err9];
        } else {
          vErrors.push(err9);
        }
        errors++;
      }
      var _valid1 = _errs12 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err10 = {
          instancePath: instancePath + '/concession_max_days',
          schemaPath: '#/properties/concession_max_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      } else {
        errors = _errs9;
        if (vErrors !== null) {
          if (_errs9) {
            vErrors.length = _errs9;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.concession_default_days !== undefined) {
      let data2 = data.concession_default_days;
      const _errs15 = errors;
      let valid3 = false;
      const _errs16 = errors;
      if (!(typeof data2 == 'number' && !(data2 % 1) && !isNaN(data2) && isFinite(data2))) {
        const err11 = {
          instancePath: instancePath + '/concession_default_days',
          schemaPath: '#/properties/concession_default_days/anyOf/0/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err11];
        } else {
          vErrors.push(err11);
        }
        errors++;
      }
      if (typeof data2 == 'number' && isFinite(data2)) {
        if (data2 > 90 || isNaN(data2)) {
          const err12 = {
            instancePath: instancePath + '/concession_default_days',
            schemaPath: '#/properties/concession_default_days/anyOf/0/maximum',
            keyword: 'maximum',
            params: { comparison: '<=', limit: 90 },
          };
          if (vErrors === null) {
            vErrors = [err12];
          } else {
            vErrors.push(err12);
          }
          errors++;
        }
        if (data2 < 1 || isNaN(data2)) {
          const err13 = {
            instancePath: instancePath + '/concession_default_days',
            schemaPath: '#/properties/concession_default_days/anyOf/0/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err13];
          } else {
            vErrors.push(err13);
          }
          errors++;
        }
      }
      var _valid2 = _errs16 === errors;
      valid3 = valid3 || _valid2;
      const _errs18 = errors;
      if (data2 !== null) {
        const err14 = {
          instancePath: instancePath + '/concession_default_days',
          schemaPath: '#/properties/concession_default_days/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err14];
        } else {
          vErrors.push(err14);
        }
        errors++;
      }
      var _valid2 = _errs18 === errors;
      valid3 = valid3 || _valid2;
      if (!valid3) {
        const err15 = {
          instancePath: instancePath + '/concession_default_days',
          schemaPath: '#/properties/concession_default_days/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err15];
        } else {
          vErrors.push(err15);
        }
        errors++;
      } else {
        errors = _errs15;
        if (vErrors !== null) {
          if (_errs15) {
            vErrors.length = _errs15;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err16 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err16];
    } else {
      vErrors.push(err16);
    }
    errors++;
  }
  validate366.errors = vErrors;
  return errors === 0;
}
validate366.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate365(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate365.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate366(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate366.errors : vErrors.concat(validate366.errors);
    errors = vErrors.length;
  }
  validate365.errors = vErrors;
  return errors === 0;
}
validate365.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__upload_grant_loop_actions__action_id__attachments_upload_grant_post =
  validate368;
const schema217 = { $ref: 'components.json#/$defs/UploadGrantRequest' };
const schema218 = {
  properties: {
    content_type: { type: 'string', maxLength: 255, minLength: 1 },
    size_bytes: { type: 'integer', minimum: 1 },
    sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['content_type', 'size_bytes', 'sha256'],
};

function validate369(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate369.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.content_type === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'content_type' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    if (data.size_bytes === undefined) {
      const err1 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'size_bytes' },
      };
      if (vErrors === null) {
        vErrors = [err1];
      } else {
        vErrors.push(err1);
      }
      errors++;
    }
    if (data.sha256 === undefined) {
      const err2 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'sha256' },
      };
      if (vErrors === null) {
        vErrors = [err2];
      } else {
        vErrors.push(err2);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'content_type' || key0 === 'size_bytes' || key0 === 'sha256')) {
        const err3 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.content_type !== undefined) {
      let data0 = data.content_type;
      if (typeof data0 === 'string') {
        if (func1(data0) > 255) {
          const err4 = {
            instancePath: instancePath + '/content_type',
            schemaPath: '#/properties/content_type/maxLength',
            keyword: 'maxLength',
            params: { limit: 255 },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
        if (func1(data0) < 1) {
          const err5 = {
            instancePath: instancePath + '/content_type',
            schemaPath: '#/properties/content_type/minLength',
            keyword: 'minLength',
            params: { limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err5];
          } else {
            vErrors.push(err5);
          }
          errors++;
        }
      } else {
        const err6 = {
          instancePath: instancePath + '/content_type',
          schemaPath: '#/properties/content_type/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
    if (data.size_bytes !== undefined) {
      let data1 = data.size_bytes;
      if (!(typeof data1 == 'number' && !(data1 % 1) && !isNaN(data1) && isFinite(data1))) {
        const err7 = {
          instancePath: instancePath + '/size_bytes',
          schemaPath: '#/properties/size_bytes/type',
          keyword: 'type',
          params: { type: 'integer' },
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      }
      if (typeof data1 == 'number' && isFinite(data1)) {
        if (data1 < 1 || isNaN(data1)) {
          const err8 = {
            instancePath: instancePath + '/size_bytes',
            schemaPath: '#/properties/size_bytes/minimum',
            keyword: 'minimum',
            params: { comparison: '>=', limit: 1 },
          };
          if (vErrors === null) {
            vErrors = [err8];
          } else {
            vErrors.push(err8);
          }
          errors++;
        }
      }
    }
    if (data.sha256 !== undefined) {
      let data2 = data.sha256;
      if (typeof data2 === 'string') {
        if (!pattern3.test(data2)) {
          const err9 = {
            instancePath: instancePath + '/sha256',
            schemaPath: '#/properties/sha256/pattern',
            keyword: 'pattern',
            params: { pattern: '^[0-9a-f]{64}$' },
          };
          if (vErrors === null) {
            vErrors = [err9];
          } else {
            vErrors.push(err9);
          }
          errors++;
        }
      } else {
        const err10 = {
          instancePath: instancePath + '/sha256',
          schemaPath: '#/properties/sha256/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err10];
        } else {
          vErrors.push(err10);
        }
        errors++;
      }
    }
  } else {
    const err11 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err11];
    } else {
      vErrors.push(err11);
    }
    errors++;
  }
  validate369.errors = vErrors;
  return errors === 0;
}
validate369.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate368(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate368.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate369(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate369.errors : vErrors.concat(validate369.errors);
    errors = vErrors.length;
  }
  validate368.errors = vErrors;
  return errors === 0;
}
validate368.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__verify_integrity_verify_post = validate371;
const schema219 = { $ref: 'components.json#/$defs/VerifyRequest' };
const schema220 = {
  properties: {
    kind: { type: 'string', enum: ['ledger', 'audit'] },
    plant_id: { anyOf: [{ type: 'string', format: 'uuid' }, { type: 'null' }] },
  },
  additionalProperties: false,
  type: 'object',
  required: ['kind'],
};

function validate372(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate372.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.kind === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'kind' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'kind' || key0 === 'plant_id')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.kind !== undefined) {
      let data0 = data.kind;
      if (typeof data0 !== 'string') {
        const err2 = {
          instancePath: instancePath + '/kind',
          schemaPath: '#/properties/kind/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      if (!(data0 === 'ledger' || data0 === 'audit')) {
        const err3 = {
          instancePath: instancePath + '/kind',
          schemaPath: '#/properties/kind/enum',
          keyword: 'enum',
          params: { allowedValues: schema220.properties.kind.enum },
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      }
    }
    if (data.plant_id !== undefined) {
      let data1 = data.plant_id;
      const _errs5 = errors;
      let valid1 = false;
      const _errs6 = errors;
      if (typeof data1 === 'string') {
        if (!formats0.test(data1)) {
          const err4 = {
            instancePath: instancePath + '/plant_id',
            schemaPath: '#/properties/plant_id/anyOf/0/format',
            keyword: 'format',
            params: { format: 'uuid' },
          };
          if (vErrors === null) {
            vErrors = [err4];
          } else {
            vErrors.push(err4);
          }
          errors++;
        }
      } else {
        const err5 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/0/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      }
      var _valid0 = _errs6 === errors;
      valid1 = valid1 || _valid0;
      const _errs8 = errors;
      if (data1 !== null) {
        const err6 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
      var _valid0 = _errs8 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err7 = {
          instancePath: instancePath + '/plant_id',
          schemaPath: '#/properties/plant_id/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err7];
        } else {
          vErrors.push(err7);
        }
        errors++;
      } else {
        errors = _errs5;
        if (vErrors !== null) {
          if (_errs5) {
            vErrors.length = _errs5;
          } else {
            vErrors = null;
          }
        }
      }
    }
  } else {
    const err8 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err8];
    } else {
      vErrors.push(err8);
    }
    errors++;
  }
  validate372.errors = vErrors;
  return errors === 0;
}
validate372.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate371(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate371.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate372(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate372.errors : vErrors.concat(validate372.errors);
    errors = vErrors.length;
  }
  validate371.errors = vErrors;
  return errors === 0;
}
validate371.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

export const body__windows_zones__zone_id__windows_put = validate374;
const schema221 = { $ref: 'components.json#/$defs/WindowsBody' };
const schema222 = {
  properties: {
    clip_window: { anyOf: [{ $ref: 'components.json#/$defs/ClipWindowBody' }, { type: 'null' }] },
    episode: { anyOf: [{ $ref: 'components.json#/$defs/EpisodeBody' }, { type: 'null' }] },
    reason_es: { type: 'string' },
  },
  additionalProperties: false,
  type: 'object',
  required: ['reason_es'],
};

function validate375(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate375.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (data && typeof data == 'object' && !Array.isArray(data)) {
    if (data.reason_es === undefined) {
      const err0 = {
        instancePath,
        schemaPath: '#/required',
        keyword: 'required',
        params: { missingProperty: 'reason_es' },
      };
      if (vErrors === null) {
        vErrors = [err0];
      } else {
        vErrors.push(err0);
      }
      errors++;
    }
    for (const key0 in data) {
      if (!(key0 === 'clip_window' || key0 === 'episode' || key0 === 'reason_es')) {
        const err1 = {
          instancePath,
          schemaPath: '#/additionalProperties',
          keyword: 'additionalProperties',
          params: { additionalProperty: key0 },
        };
        if (vErrors === null) {
          vErrors = [err1];
        } else {
          vErrors.push(err1);
        }
        errors++;
      }
    }
    if (data.clip_window !== undefined) {
      let data0 = data.clip_window;
      const _errs3 = errors;
      let valid1 = false;
      const _errs4 = errors;
      if (
        !validate232(data0, {
          instancePath: instancePath + '/clip_window',
          parentData: data,
          parentDataProperty: 'clip_window',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate232.errors : vErrors.concat(validate232.errors);
        errors = vErrors.length;
      }
      var _valid0 = _errs4 === errors;
      valid1 = valid1 || _valid0;
      const _errs5 = errors;
      if (data0 !== null) {
        const err2 = {
          instancePath: instancePath + '/clip_window',
          schemaPath: '#/properties/clip_window/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err2];
        } else {
          vErrors.push(err2);
        }
        errors++;
      }
      var _valid0 = _errs5 === errors;
      valid1 = valid1 || _valid0;
      if (!valid1) {
        const err3 = {
          instancePath: instancePath + '/clip_window',
          schemaPath: '#/properties/clip_window/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err3];
        } else {
          vErrors.push(err3);
        }
        errors++;
      } else {
        errors = _errs3;
        if (vErrors !== null) {
          if (_errs3) {
            vErrors.length = _errs3;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.episode !== undefined) {
      let data1 = data.episode;
      const _errs8 = errors;
      let valid2 = false;
      const _errs9 = errors;
      if (
        !validate234(data1, {
          instancePath: instancePath + '/episode',
          parentData: data,
          parentDataProperty: 'episode',
          rootData,
          dynamicAnchors,
        })
      ) {
        vErrors = vErrors === null ? validate234.errors : vErrors.concat(validate234.errors);
        errors = vErrors.length;
      }
      var _valid1 = _errs9 === errors;
      valid2 = valid2 || _valid1;
      const _errs10 = errors;
      if (data1 !== null) {
        const err4 = {
          instancePath: instancePath + '/episode',
          schemaPath: '#/properties/episode/anyOf/1/type',
          keyword: 'type',
          params: { type: 'null' },
        };
        if (vErrors === null) {
          vErrors = [err4];
        } else {
          vErrors.push(err4);
        }
        errors++;
      }
      var _valid1 = _errs10 === errors;
      valid2 = valid2 || _valid1;
      if (!valid2) {
        const err5 = {
          instancePath: instancePath + '/episode',
          schemaPath: '#/properties/episode/anyOf',
          keyword: 'anyOf',
          params: {},
        };
        if (vErrors === null) {
          vErrors = [err5];
        } else {
          vErrors.push(err5);
        }
        errors++;
      } else {
        errors = _errs8;
        if (vErrors !== null) {
          if (_errs8) {
            vErrors.length = _errs8;
          } else {
            vErrors = null;
          }
        }
      }
    }
    if (data.reason_es !== undefined) {
      if (typeof data.reason_es !== 'string') {
        const err6 = {
          instancePath: instancePath + '/reason_es',
          schemaPath: '#/properties/reason_es/type',
          keyword: 'type',
          params: { type: 'string' },
        };
        if (vErrors === null) {
          vErrors = [err6];
        } else {
          vErrors.push(err6);
        }
        errors++;
      }
    }
  } else {
    const err7 = {
      instancePath,
      schemaPath: '#/type',
      keyword: 'type',
      params: { type: 'object' },
    };
    if (vErrors === null) {
      vErrors = [err7];
    } else {
      vErrors.push(err7);
    }
    errors++;
  }
  validate375.errors = vErrors;
  return errors === 0;
}
validate375.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

function validate374(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  let vErrors = null;
  let errors = 0;
  const evaluated0 = validate374.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (
    !validate375(data, { instancePath, parentData, parentDataProperty, rootData, dynamicAnchors })
  ) {
    vErrors = vErrors === null ? validate375.errors : vErrors.concat(validate375.errors);
    errors = vErrors.length;
  }
  validate374.errors = vErrors;
  return errors === 0;
}
validate374.evaluated = { props: true, dynamicProps: false, dynamicItems: false };
export const bodies = Object.freeze({
  accept_invitations__token__accept_post: body__accept_invitations__token__accept_post,
  accept_notice_privacy_notice_accept_post: body__accept_notice_privacy_notice_accept_post,
  add_evidence_loop_actions__action_id__evidence_post:
    body__add_evidence_loop_actions__action_id__evidence_post,
  assign_loop_findings__finding_state_id__actions_post:
    body__assign_loop_findings__finding_state_id__actions_post,
  assign_role_users__user_id__roles_post: body__assign_role_users__user_id__roles_post,
  assign_zone_nodes__node_id__zones_post: body__assign_zone_nodes__node_id__zones_post,
  cameras_zones__zone_id__cameras_put: body__cameras_zones__zone_id__cameras_put,
  change_configuration_loop_plants__plant_id__configuration_put:
    body__change_configuration_loop_plants__plant_id__configuration_put,
  change_password_auth_password_post: body__change_password_auth_password_post,
  change_vocabulary_loop_vocabulary_changes_post:
    body__change_vocabulary_loop_vocabulary_changes_post,
  classify_loop_findings__finding_state_id__classifications_post:
    body__classify_loop_findings__finding_state_id__classifications_post,
  close_loop_findings__finding_state_id__closure_post:
    body__close_loop_findings__finding_state_id__closure_post,
  close_step_walk_tests__session_id__steps__step_id__close_post:
    body__close_step_walk_tests__session_id__steps__step_id__close_post,
  close_walk_test_walk_tests__session_id__close_post:
    body__close_walk_test_walk_tests__session_id__close_post,
  create_agreement_zones__zone_id__use_agreements_post:
    body__create_agreement_zones__zone_id__use_agreements_post,
  create_plant_plants_post: body__create_plant_plants_post,
  create_zone_plants__plant_id__zones_post: body__create_zone_plants__plant_id__zones_post,
  declare_loop_findings__finding_state_id__unclosable_post:
    body__declare_loop_findings__finding_state_id__unclosable_post,
  declare_node_plants__plant_id__nodes_post: body__declare_node_plants__plant_id__nodes_post,
  decommission_node_nodes__node_id__decommission_post:
    body__decommission_node_nodes__node_id__decommission_post,
  enroll_auth_second_factor_enroll_post: body__enroll_auth_second_factor_enroll_post,
  evaluate_plants__plant_id__admissions_post: body__evaluate_plants__plant_id__admissions_post,
  framing_recapture_zones__zone_id__framing_recaptures_post:
    body__framing_recapture_zones__zone_id__framing_recaptures_post,
  grant_document_documents_post: body__grant_document_documents_post,
  grant_provider_concessions_post: body__grant_provider_concessions_post,
  invite_users_post: body__invite_users_post,
  login_auth_login_post: body__login_auth_login_post,
  minimum_coverage_zones__zone_id__minimum_coverage_put:
    body__minimum_coverage_zones__zone_id__minimum_coverage_put,
  new_standard_version_zones__zone_id__standards__standard_id__versions_post:
    body__new_standard_version_zones__zone_id__standards__standard_id__versions_post,
  new_standard_zones__zone_id__standards_post: body__new_standard_zones__zone_id__standards_post,
  open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post:
    body__open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post,
  open_walk_test_zones__zone_id__walk_tests_post:
    body__open_walk_test_zones__zone_id__walk_tests_post,
  publish_target_version_fleet_target_versions_post:
    body__publish_target_version_fleet_target_versions_post,
  put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put:
    body__put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put,
  put_signatory_policy_plants__plant_id__signatory_policy_put:
    body__put_signatory_policy_plants__plant_id__signatory_policy_put,
  put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put:
    body__put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put,
  reactivate_users__user_id__reactivate_post: body__reactivate_users__user_id__reactivate_post,
  record_commitments_accreditation_monthly_packages__package_id__commitments_post:
    body__record_commitments_accreditation_monthly_packages__package_id__commitments_post,
  record_exposure_sample_walk_tests__session_id__exposure_samples_post:
    body__record_exposure_sample_walk_tests__session_id__exposure_samples_post,
  record_occlusion_test_walk_tests__session_id__occlusion_tests_post:
    body__record_occlusion_test_walk_tests__session_id__occlusion_tests_post,
  record_pass_walk_tests__session_id__passes_post:
    body__record_pass_walk_tests__session_id__passes_post,
  record_sample_result_accreditation_sample_items__sample_item_id__result_post:
    body__record_sample_result_accreditation_sample_items__sample_item_id__result_post,
  reopen_loop_findings__finding_state_id__reopening_post:
    body__reopen_loop_findings__finding_state_id__reopening_post,
  reopen_walk_test_walk_tests__session_id__reopen_post:
    body__reopen_walk_test_walk_tests__session_id__reopen_post,
  request_export_accreditation_exports_post: body__request_export_accreditation_exports_post,
  resolve_loop_review_queue__detection_id__resolution_post:
    body__resolve_loop_review_queue__detection_id__resolution_post,
  retirement_zones__zone_id__standards__standard_id__retirement_post:
    body__retirement_zones__zone_id__standards__standard_id__retirement_post,
  review_loop_unclosable__declaration_id__reviews_post:
    body__review_loop_unclosable__declaration_id__reviews_post,
  revocation_zones__zone_id__gates__gate__revocation_post:
    body__revocation_zones__zone_id__gates__gate__revocation_post,
  revoke_node_nodes__node_id__revocation_post: body__revoke_node_nodes__node_id__revocation_post,
  scope_record_zones__zone_id__gates_mounting_scope_record_post:
    body__scope_record_zones__zone_id__gates_mounting_scope_record_post,
  second_factor_auth_second_factor_post: body__second_factor_auth_second_factor_post,
  sign_policy_plants__plant_id__policy_post: body__sign_policy_plants__plant_id__policy_post,
  signals_zones__zone_id__signals_put: body__signals_zones__zone_id__signals_put,
  signature_loop_root_cause_analyses__analysis_id__signature_post:
    body__signature_loop_root_cause_analyses__analysis_id__signature_post,
  single_occupancy_zones__zone_id__catalog_single_occupancy_put:
    body__single_occupancy_zones__zone_id__catalog_single_occupancy_put,
  start_step_walk_tests__session_id__steps_post:
    body__start_step_walk_tests__session_id__steps_post,
  thresholds_zones__zone_id__thresholds_put: body__thresholds_zones__zone_id__thresholds_put,
  unassign_zone_nodes__node_id__zones__zone_id__unassignment_post:
    body__unassign_zone_nodes__node_id__zones__zone_id__unassignment_post,
  update_commitment_accreditation_commitments__commitment_id__patch:
    body__update_commitment_accreditation_commitments__commitment_id__patch,
  update_preferences_notifications_preferences_put:
    body__update_preferences_notifications_preferences_put,
  update_profile_users__user_id__patch: body__update_profile_users__user_id__patch,
  update_settings_organization_settings_patch: body__update_settings_organization_settings_patch,
  upload_grant_loop_actions__action_id__attachments_upload_grant_post:
    body__upload_grant_loop_actions__action_id__attachments_upload_grant_post,
  verify_integrity_verify_post: body__verify_integrity_verify_post,
  windows_zones__zone_id__windows_put: body__windows_zones__zone_id__windows_put,
});
export const queries = Object.freeze({
  catalog_history_zones__zone_id__catalog_versions_get:
    query__catalog_history_zones__zone_id__catalog_versions_get,
  commissioning_clips_zones__zone_id__commissioning_clips_get:
    query__commissioning_clips_zones__zone_id__commissioning_clips_get,
  fleet_node_fleet_nodes__node_id__get: query__fleet_node_fleet_nodes__node_id__get,
  get_adoption_accreditation_adoption_get: query__get_adoption_accreditation_adoption_get,
  get_exposure_accreditation_exposure_get: query__get_exposure_accreditation_exposure_get,
  get_monthly_package_accreditation_monthly_packages_get:
    query__get_monthly_package_accreditation_monthly_packages_get,
  list_admissions_plants__plant_id__admissions_get:
    query__list_admissions_plants__plant_id__admissions_get,
  list_audit_entries_audit_entries_get: query__list_audit_entries_audit_entries_get,
  list_commitments_accreditation_commitments_get:
    query__list_commitments_accreditation_commitments_get,
  list_concessions_concessions_get: query__list_concessions_concessions_get,
  list_enrollment_attempts_nodes__node_id__enrollment_attempts_get:
    query__list_enrollment_attempts_nodes__node_id__enrollment_attempts_get,
  list_findings_loop_findings_get: query__list_findings_loop_findings_get,
  list_fleet_alarms_fleet_alarms_get: query__list_fleet_alarms_fleet_alarms_get,
  list_fleet_nodes_fleet_nodes_get: query__list_fleet_nodes_fleet_nodes_get,
  list_labels_labels_get: query__list_labels_labels_get,
  list_notifications_notifications_get: query__list_notifications_notifications_get,
  list_overdue_loop_overdue_get: query__list_overdue_loop_overdue_get,
  list_queries_concessions__concession_id__queries_get:
    query__list_queries_concessions__concession_id__queries_get,
  list_records_ledger_records_get: query__list_records_ledger_records_get,
  list_review_queue_loop_review_queue_get: query__list_review_queue_loop_review_queue_get,
  list_users_users_get: query__list_users_users_get,
  metrics_accreditation_metrics_get: query__metrics_accreditation_metrics_get,
  status_at_zones__zone_id__coverage_at_get: query__status_at_zones__zone_id__coverage_at_get,
  timeline_zones__zone_id__coverage_get: query__timeline_zones__zone_id__coverage_get,
});

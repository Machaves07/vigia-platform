/* eslint-disable -- código autónomo de Ajv: no se edita ni se revisa a mano. */
// @ts-nocheck -- código autónomo de Ajv; sus tipos están en uiPreferences.validator.d.ts.
// Generado por `npm run generate` (scripts/generate.mjs) desde src/state/scope/uiPreferences.schema.json: no se edita a mano; `npm run generate:check` exige diferencia vacía (NFR-APP-48).
import * as runtime0Module from 'ajv/dist/runtime/equal.js';
const cjs = (module) => (module.default?.__esModule ? module.default : module);
const runtime0 = cjs(runtime0Module);
export const validateUiPreferences = validate20;
const schema31 = {
  $id: 'ui_preferences.json',
  $comment:
    'UiPreferences (domain-entities §2.12, BR-APP-19, LC-APP-07): único contenido admitido en el almacenamiento del navegador. Esquema cerrado: sin campos fuera de la lista, sin texto libre, sin identificadores de registro ni tokens (solo la planta seleccionada como UUID). Sin `$schema`: el Ajv de 2020-12 ya usa ese dialecto. `npm run generate` lo compila en código autónomo (`uiPreferences.validator.js`).',
  title: 'UiPreferences',
  type: 'object',
  additionalProperties: false,
  properties: {
    selected_plant_id: {
      type: 'string',
      pattern: '^(?:all|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
    },
    list_filters: {
      type: 'object',
      propertyNames: { $ref: '#/$defs/screen' },
      additionalProperties: {
        type: 'object',
        maxProperties: 20,
        propertyNames: { $ref: '#/$defs/token' },
        additionalProperties: { $ref: '#/$defs/token' },
      },
    },
    columns: {
      type: 'object',
      propertyNames: { $ref: '#/$defs/screen' },
      additionalProperties: {
        type: 'array',
        maxItems: 50,
        uniqueItems: true,
        items: { $ref: '#/$defs/token' },
      },
    },
    active_tabs: {
      type: 'object',
      propertyNames: { $ref: '#/$defs/screen' },
      additionalProperties: { $ref: '#/$defs/token' },
    },
  },
  $defs: {
    screen: {
      type: 'string',
      enum: [
        'shell',
        'home',
        'scr01',
        'scr02',
        'scr03',
        'scr04',
        'scr05',
        'scr06',
        'scr07',
        'scr08',
        'scr09',
        'scr10',
        'scr11',
        'scr12',
        'scr13',
        'scr14',
        'scr15',
        'scr16',
      ],
    },
    token: {
      $comment:
        'Valor de lista cerrada, nombre de filtro, columna o pestaña: `snake_case` de 1 a 64 caracteres. Excluye UUID, espacios, mayúsculas y puntos (texto libre, identificadores de registro y tokens).',
      type: 'string',
      pattern: '^[a-z][a-z0-9_]{0,63}$',
    },
  },
};
const schema32 = {
  type: 'string',
  enum: [
    'shell',
    'home',
    'scr01',
    'scr02',
    'scr03',
    'scr04',
    'scr05',
    'scr06',
    'scr07',
    'scr08',
    'scr09',
    'scr10',
    'scr11',
    'scr12',
    'scr13',
    'scr14',
    'scr15',
    'scr16',
  ],
};
const schema33 = {
  $comment:
    'Valor de lista cerrada, nombre de filtro, columna o pestaña: `snake_case` de 1 a 64 caracteres. Excluye UUID, espacios, mayúsculas y puntos (texto libre, identificadores de registro y tokens).',
  type: 'string',
  pattern: '^[a-z][a-z0-9_]{0,63}$',
};
const pattern4 = new RegExp(
  '^(?:all|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
  'u',
);
const pattern5 = new RegExp('^[a-z][a-z0-9_]{0,63}$', 'u');
const func0 = runtime0.default;

function validate20(
  data,
  { instancePath = '', parentData, parentDataProperty, rootData = data, dynamicAnchors = {} } = {},
) {
  /*# sourceURL="ui_preferences.json" */ let vErrors = null;
  let errors = 0;
  const evaluated0 = validate20.evaluated;
  if (evaluated0.dynamicProps) {
    evaluated0.props = undefined;
  }
  if (evaluated0.dynamicItems) {
    evaluated0.items = undefined;
  }
  if (errors === 0) {
    if (data && typeof data == 'object' && !Array.isArray(data)) {
      const _errs2 = errors;
      for (const key0 in data) {
        if (!(
          key0 === 'selected_plant_id' ||
          key0 === 'list_filters' ||
          key0 === 'columns' ||
          key0 === 'active_tabs'
        )) {
          validate20.errors = [
            {
              instancePath,
              schemaPath: '#/additionalProperties',
              keyword: 'additionalProperties',
              params: { additionalProperty: key0 },
            },
          ];
          return false;
          break;
        }
      }
      if (_errs2 === errors) {
        if (data.selected_plant_id !== undefined) {
          let data0 = data.selected_plant_id;
          const _errs3 = errors;
          if (errors === _errs3) {
            if (typeof data0 === 'string') {
              if (!pattern4.test(data0)) {
                validate20.errors = [
                  {
                    instancePath: instancePath + '/selected_plant_id',
                    schemaPath: '#/properties/selected_plant_id/pattern',
                    keyword: 'pattern',
                    params: {
                      pattern:
                        '^(?:all|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
                    },
                  },
                ];
                return false;
              }
            } else {
              validate20.errors = [
                {
                  instancePath: instancePath + '/selected_plant_id',
                  schemaPath: '#/properties/selected_plant_id/type',
                  keyword: 'type',
                  params: { type: 'string' },
                },
              ];
              return false;
            }
          }
          var valid0 = _errs3 === errors;
        } else {
          var valid0 = true;
        }
        if (valid0) {
          if (data.list_filters !== undefined) {
            let data1 = data.list_filters;
            const _errs5 = errors;
            if (errors === _errs5) {
              if (data1 && typeof data1 == 'object' && !Array.isArray(data1)) {
                for (const key1 in data1) {
                  const _errs7 = errors;
                  if (typeof key1 !== 'string') {
                    const err0 = {
                      instancePath: instancePath + '/list_filters',
                      schemaPath: '#/$defs/screen/type',
                      keyword: 'type',
                      params: { type: 'string' },
                      propertyName: key1,
                    };
                    if (vErrors === null) {
                      vErrors = [err0];
                    } else {
                      vErrors.push(err0);
                    }
                    errors++;
                  }
                  if (!(
                    key1 === 'shell' ||
                    key1 === 'home' ||
                    key1 === 'scr01' ||
                    key1 === 'scr02' ||
                    key1 === 'scr03' ||
                    key1 === 'scr04' ||
                    key1 === 'scr05' ||
                    key1 === 'scr06' ||
                    key1 === 'scr07' ||
                    key1 === 'scr08' ||
                    key1 === 'scr09' ||
                    key1 === 'scr10' ||
                    key1 === 'scr11' ||
                    key1 === 'scr12' ||
                    key1 === 'scr13' ||
                    key1 === 'scr14' ||
                    key1 === 'scr15' ||
                    key1 === 'scr16'
                  )) {
                    const err1 = {
                      instancePath: instancePath + '/list_filters',
                      schemaPath: '#/$defs/screen/enum',
                      keyword: 'enum',
                      params: { allowedValues: schema32.enum },
                      propertyName: key1,
                    };
                    if (vErrors === null) {
                      vErrors = [err1];
                    } else {
                      vErrors.push(err1);
                    }
                    errors++;
                  }
                  var valid1 = _errs7 === errors;
                  if (!valid1) {
                    const err2 = {
                      instancePath: instancePath + '/list_filters',
                      schemaPath: '#/properties/list_filters/propertyNames',
                      keyword: 'propertyNames',
                      params: { propertyName: key1 },
                    };
                    if (vErrors === null) {
                      vErrors = [err2];
                    } else {
                      vErrors.push(err2);
                    }
                    errors++;
                    validate20.errors = vErrors;
                    return false;
                    break;
                  }
                }
                if (valid1) {
                  for (const key2 in data1) {
                    let data2 = data1[key2];
                    const _errs11 = errors;
                    if (errors === _errs11) {
                      if (data2 && typeof data2 == 'object' && !Array.isArray(data2)) {
                        if (Object.keys(data2).length > 20) {
                          validate20.errors = [
                            {
                              instancePath:
                                instancePath +
                                '/list_filters/' +
                                key2.replace(/~/g, '~0').replace(/\//g, '~1'),
                              schemaPath:
                                '#/properties/list_filters/additionalProperties/maxProperties',
                              keyword: 'maxProperties',
                              params: { limit: 20 },
                            },
                          ];
                          return false;
                        } else {
                          for (const key3 in data2) {
                            const _errs13 = errors;
                            const _errs14 = errors;
                            if (errors === _errs14) {
                              if (typeof key3 === 'string') {
                                if (!pattern5.test(key3)) {
                                  const err3 = {
                                    instancePath:
                                      instancePath +
                                      '/list_filters/' +
                                      key2.replace(/~/g, '~0').replace(/\//g, '~1'),
                                    schemaPath: '#/$defs/token/pattern',
                                    keyword: 'pattern',
                                    params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
                                    propertyName: key3,
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
                                  instancePath:
                                    instancePath +
                                    '/list_filters/' +
                                    key2.replace(/~/g, '~0').replace(/\//g, '~1'),
                                  schemaPath: '#/$defs/token/type',
                                  keyword: 'type',
                                  params: { type: 'string' },
                                  propertyName: key3,
                                };
                                if (vErrors === null) {
                                  vErrors = [err4];
                                } else {
                                  vErrors.push(err4);
                                }
                                errors++;
                              }
                            }
                            var valid4 = _errs13 === errors;
                            if (!valid4) {
                              const err5 = {
                                instancePath:
                                  instancePath +
                                  '/list_filters/' +
                                  key2.replace(/~/g, '~0').replace(/\//g, '~1'),
                                schemaPath:
                                  '#/properties/list_filters/additionalProperties/propertyNames',
                                keyword: 'propertyNames',
                                params: { propertyName: key3 },
                              };
                              if (vErrors === null) {
                                vErrors = [err5];
                              } else {
                                vErrors.push(err5);
                              }
                              errors++;
                              validate20.errors = vErrors;
                              return false;
                              break;
                            }
                          }
                          if (valid4) {
                            for (const key4 in data2) {
                              let data3 = data2[key4];
                              const _errs18 = errors;
                              const _errs19 = errors;
                              if (errors === _errs19) {
                                if (typeof data3 === 'string') {
                                  if (!pattern5.test(data3)) {
                                    validate20.errors = [
                                      {
                                        instancePath:
                                          instancePath +
                                          '/list_filters/' +
                                          key2.replace(/~/g, '~0').replace(/\//g, '~1') +
                                          '/' +
                                          key4.replace(/~/g, '~0').replace(/\//g, '~1'),
                                        schemaPath: '#/$defs/token/pattern',
                                        keyword: 'pattern',
                                        params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
                                      },
                                    ];
                                    return false;
                                  }
                                } else {
                                  validate20.errors = [
                                    {
                                      instancePath:
                                        instancePath +
                                        '/list_filters/' +
                                        key2.replace(/~/g, '~0').replace(/\//g, '~1') +
                                        '/' +
                                        key4.replace(/~/g, '~0').replace(/\//g, '~1'),
                                      schemaPath: '#/$defs/token/type',
                                      keyword: 'type',
                                      params: { type: 'string' },
                                    },
                                  ];
                                  return false;
                                }
                              }
                              var valid6 = _errs18 === errors;
                              if (!valid6) {
                                break;
                              }
                            }
                          }
                        }
                      } else {
                        validate20.errors = [
                          {
                            instancePath:
                              instancePath +
                              '/list_filters/' +
                              key2.replace(/~/g, '~0').replace(/\//g, '~1'),
                            schemaPath: '#/properties/list_filters/additionalProperties/type',
                            keyword: 'type',
                            params: { type: 'object' },
                          },
                        ];
                        return false;
                      }
                    }
                    var valid3 = _errs11 === errors;
                    if (!valid3) {
                      break;
                    }
                  }
                }
              } else {
                validate20.errors = [
                  {
                    instancePath: instancePath + '/list_filters',
                    schemaPath: '#/properties/list_filters/type',
                    keyword: 'type',
                    params: { type: 'object' },
                  },
                ];
                return false;
              }
            }
            var valid0 = _errs5 === errors;
          } else {
            var valid0 = true;
          }
          if (valid0) {
            if (data.columns !== undefined) {
              let data4 = data.columns;
              const _errs22 = errors;
              if (errors === _errs22) {
                if (data4 && typeof data4 == 'object' && !Array.isArray(data4)) {
                  for (const key5 in data4) {
                    const _errs24 = errors;
                    if (typeof key5 !== 'string') {
                      const err6 = {
                        instancePath: instancePath + '/columns',
                        schemaPath: '#/$defs/screen/type',
                        keyword: 'type',
                        params: { type: 'string' },
                        propertyName: key5,
                      };
                      if (vErrors === null) {
                        vErrors = [err6];
                      } else {
                        vErrors.push(err6);
                      }
                      errors++;
                    }
                    if (!(
                      key5 === 'shell' ||
                      key5 === 'home' ||
                      key5 === 'scr01' ||
                      key5 === 'scr02' ||
                      key5 === 'scr03' ||
                      key5 === 'scr04' ||
                      key5 === 'scr05' ||
                      key5 === 'scr06' ||
                      key5 === 'scr07' ||
                      key5 === 'scr08' ||
                      key5 === 'scr09' ||
                      key5 === 'scr10' ||
                      key5 === 'scr11' ||
                      key5 === 'scr12' ||
                      key5 === 'scr13' ||
                      key5 === 'scr14' ||
                      key5 === 'scr15' ||
                      key5 === 'scr16'
                    )) {
                      const err7 = {
                        instancePath: instancePath + '/columns',
                        schemaPath: '#/$defs/screen/enum',
                        keyword: 'enum',
                        params: { allowedValues: schema32.enum },
                        propertyName: key5,
                      };
                      if (vErrors === null) {
                        vErrors = [err7];
                      } else {
                        vErrors.push(err7);
                      }
                      errors++;
                    }
                    var valid8 = _errs24 === errors;
                    if (!valid8) {
                      const err8 = {
                        instancePath: instancePath + '/columns',
                        schemaPath: '#/properties/columns/propertyNames',
                        keyword: 'propertyNames',
                        params: { propertyName: key5 },
                      };
                      if (vErrors === null) {
                        vErrors = [err8];
                      } else {
                        vErrors.push(err8);
                      }
                      errors++;
                      validate20.errors = vErrors;
                      return false;
                      break;
                    }
                  }
                  if (valid8) {
                    for (const key6 in data4) {
                      let data5 = data4[key6];
                      const _errs28 = errors;
                      if (errors === _errs28) {
                        if (Array.isArray(data5)) {
                          if (data5.length > 50) {
                            validate20.errors = [
                              {
                                instancePath:
                                  instancePath +
                                  '/columns/' +
                                  key6.replace(/~/g, '~0').replace(/\//g, '~1'),
                                schemaPath: '#/properties/columns/additionalProperties/maxItems',
                                keyword: 'maxItems',
                                params: { limit: 50 },
                              },
                            ];
                            return false;
                          } else {
                            var valid11 = true;
                            const len0 = data5.length;
                            for (let i0 = 0; i0 < len0; i0++) {
                              let data6 = data5[i0];
                              const _errs30 = errors;
                              const _errs31 = errors;
                              if (errors === _errs31) {
                                if (typeof data6 === 'string') {
                                  if (!pattern5.test(data6)) {
                                    validate20.errors = [
                                      {
                                        instancePath:
                                          instancePath +
                                          '/columns/' +
                                          key6.replace(/~/g, '~0').replace(/\//g, '~1') +
                                          '/' +
                                          i0,
                                        schemaPath: '#/$defs/token/pattern',
                                        keyword: 'pattern',
                                        params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
                                      },
                                    ];
                                    return false;
                                  }
                                } else {
                                  validate20.errors = [
                                    {
                                      instancePath:
                                        instancePath +
                                        '/columns/' +
                                        key6.replace(/~/g, '~0').replace(/\//g, '~1') +
                                        '/' +
                                        i0,
                                      schemaPath: '#/$defs/token/type',
                                      keyword: 'type',
                                      params: { type: 'string' },
                                    },
                                  ];
                                  return false;
                                }
                              }
                              var valid11 = _errs30 === errors;
                              if (!valid11) {
                                break;
                              }
                            }
                            if (valid11) {
                              let i1 = data5.length;
                              let j0;
                              if (i1 > 1) {
                                outer0: for (; i1--;) {
                                  for (j0 = i1; j0--;) {
                                    if (func0(data5[i1], data5[j0])) {
                                      validate20.errors = [
                                        {
                                          instancePath:
                                            instancePath +
                                            '/columns/' +
                                            key6.replace(/~/g, '~0').replace(/\//g, '~1'),
                                          schemaPath:
                                            '#/properties/columns/additionalProperties/uniqueItems',
                                          keyword: 'uniqueItems',
                                          params: { i: i1, j: j0 },
                                        },
                                      ];
                                      return false;
                                      break outer0;
                                    }
                                  }
                                }
                              }
                            }
                          }
                        } else {
                          validate20.errors = [
                            {
                              instancePath:
                                instancePath +
                                '/columns/' +
                                key6.replace(/~/g, '~0').replace(/\//g, '~1'),
                              schemaPath: '#/properties/columns/additionalProperties/type',
                              keyword: 'type',
                              params: { type: 'array' },
                            },
                          ];
                          return false;
                        }
                      }
                      var valid10 = _errs28 === errors;
                      if (!valid10) {
                        break;
                      }
                    }
                  }
                } else {
                  validate20.errors = [
                    {
                      instancePath: instancePath + '/columns',
                      schemaPath: '#/properties/columns/type',
                      keyword: 'type',
                      params: { type: 'object' },
                    },
                  ];
                  return false;
                }
              }
              var valid0 = _errs22 === errors;
            } else {
              var valid0 = true;
            }
            if (valid0) {
              if (data.active_tabs !== undefined) {
                let data7 = data.active_tabs;
                const _errs34 = errors;
                if (errors === _errs34) {
                  if (data7 && typeof data7 == 'object' && !Array.isArray(data7)) {
                    for (const key7 in data7) {
                      const _errs36 = errors;
                      if (typeof key7 !== 'string') {
                        const err9 = {
                          instancePath: instancePath + '/active_tabs',
                          schemaPath: '#/$defs/screen/type',
                          keyword: 'type',
                          params: { type: 'string' },
                          propertyName: key7,
                        };
                        if (vErrors === null) {
                          vErrors = [err9];
                        } else {
                          vErrors.push(err9);
                        }
                        errors++;
                      }
                      if (!(
                        key7 === 'shell' ||
                        key7 === 'home' ||
                        key7 === 'scr01' ||
                        key7 === 'scr02' ||
                        key7 === 'scr03' ||
                        key7 === 'scr04' ||
                        key7 === 'scr05' ||
                        key7 === 'scr06' ||
                        key7 === 'scr07' ||
                        key7 === 'scr08' ||
                        key7 === 'scr09' ||
                        key7 === 'scr10' ||
                        key7 === 'scr11' ||
                        key7 === 'scr12' ||
                        key7 === 'scr13' ||
                        key7 === 'scr14' ||
                        key7 === 'scr15' ||
                        key7 === 'scr16'
                      )) {
                        const err10 = {
                          instancePath: instancePath + '/active_tabs',
                          schemaPath: '#/$defs/screen/enum',
                          keyword: 'enum',
                          params: { allowedValues: schema32.enum },
                          propertyName: key7,
                        };
                        if (vErrors === null) {
                          vErrors = [err10];
                        } else {
                          vErrors.push(err10);
                        }
                        errors++;
                      }
                      var valid14 = _errs36 === errors;
                      if (!valid14) {
                        const err11 = {
                          instancePath: instancePath + '/active_tabs',
                          schemaPath: '#/properties/active_tabs/propertyNames',
                          keyword: 'propertyNames',
                          params: { propertyName: key7 },
                        };
                        if (vErrors === null) {
                          vErrors = [err11];
                        } else {
                          vErrors.push(err11);
                        }
                        errors++;
                        validate20.errors = vErrors;
                        return false;
                        break;
                      }
                    }
                    if (valid14) {
                      for (const key8 in data7) {
                        let data8 = data7[key8];
                        const _errs40 = errors;
                        const _errs41 = errors;
                        if (errors === _errs41) {
                          if (typeof data8 === 'string') {
                            if (!pattern5.test(data8)) {
                              validate20.errors = [
                                {
                                  instancePath:
                                    instancePath +
                                    '/active_tabs/' +
                                    key8.replace(/~/g, '~0').replace(/\//g, '~1'),
                                  schemaPath: '#/$defs/token/pattern',
                                  keyword: 'pattern',
                                  params: { pattern: '^[a-z][a-z0-9_]{0,63}$' },
                                },
                              ];
                              return false;
                            }
                          } else {
                            validate20.errors = [
                              {
                                instancePath:
                                  instancePath +
                                  '/active_tabs/' +
                                  key8.replace(/~/g, '~0').replace(/\//g, '~1'),
                                schemaPath: '#/$defs/token/type',
                                keyword: 'type',
                                params: { type: 'string' },
                              },
                            ];
                            return false;
                          }
                        }
                        var valid16 = _errs40 === errors;
                        if (!valid16) {
                          break;
                        }
                      }
                    }
                  } else {
                    validate20.errors = [
                      {
                        instancePath: instancePath + '/active_tabs',
                        schemaPath: '#/properties/active_tabs/type',
                        keyword: 'type',
                        params: { type: 'object' },
                      },
                    ];
                    return false;
                  }
                }
                var valid0 = _errs34 === errors;
              } else {
                var valid0 = true;
              }
            }
          }
        }
      }
    } else {
      validate20.errors = [
        { instancePath, schemaPath: '#/type', keyword: 'type', params: { type: 'object' } },
      ];
      return false;
    }
  }
  validate20.errors = vErrors;
  return errors === 0;
}
validate20.evaluated = { props: true, dynamicProps: false, dynamicItems: false };

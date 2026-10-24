/**
 * Generadores de fast-check de las propiedades de U-05 (LC-APP-24): `fromSchema` sobre cualquier
 * esquema JSON 2020-12 / OpenAPI 3.1, las operaciones y esquemas de `app.yaml` y los generadores
 * de dominio. Ver cada módulo.
 */
export {
  canonicalKey,
  fromSchema,
  UnsatisfiableSchemaError,
  UnsupportedSchemaError,
} from './fromSchema';
export type { FromSchemaOptions, JsonSchema } from './fromSchema';
export {
  API_DOCUMENT,
  API_OPERATIONS,
  API_SCHEMA_NAMES,
  apiOperation,
  apiSchemaArbitrary,
  componentArbitrary,
  successStatus,
} from './openapi';
export type { ApiOperation } from './openapi';
export * from './domain';

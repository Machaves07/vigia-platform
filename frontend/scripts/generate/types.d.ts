/** Tipos de los guiones de generación (VIG-280): el documento OpenAPI leído como valor JSON. */
export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export interface JsonObject {
  [key: string]: JsonValue;
}

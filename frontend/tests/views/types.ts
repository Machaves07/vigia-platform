/**
 * Registro de una vista de lectura (RV-01 a 20) para las metapropiedades de las prohibiciones de
 * pantalla, PR-APP-07 y PR-APP-10 (NFR-APP-45).
 *
 * Cada tarea de pantalla añade `tests/views/<vista>.ts` con
 * `export default { <nombre>: registro } satisfies Record<string, ViewRegistration>`; las dos
 * propiedades recorren todas las vistas registradas sin que nadie las edite. Los archivos que
 * empiezan por `_` (la vista de autoprueba) y este archivo no entran en el registro.
 */
import type fc from 'fast-check';
import type { ReactElement } from 'react';

/** Vista de lectura del diseño (`domain-entities.md` §3): `RV-01` a `RV-20`. */
type ReadViewId = `RV-${string}`;

/** Pantalla que muestra la vista: `SCR-01` a `SCR-16`. */
type ScreenId = `SCR-${string}`;

/** Error de contrato que la vista registra en lugar de pintar el dato (BR-APP-30). */
export interface ViewContractError {
  /** Código cerrado del error, p. ej. `foreign_organization`. */
  readonly code: string;
  /** Ruta del registro rechazado (`items[2]`), si la vista la conoce. */
  readonly field?: string | null;
}

/** Contexto de sesión con que la propiedad pinta la vista. */
export interface ViewRenderContext {
  /** Organización del `Principal`. */
  readonly organizationId: string;
  /** Organización cliente de la concesión activa del proveedor, o `null` sin concesión. */
  readonly concessionOrganizationId: string | null;
  /** Registra un dato de otra organización como error de contrato, sin mostrarlo. */
  readonly reportContractError: (error: ViewContractError) => void;
}

export interface ViewRegistration {
  readonly screen: ScreenId;
  readonly view: ReadViewId;
  /**
   * Datos de la vista tal como llegan del servidor, normalmente `readView('<Esquema>')` de
   * `tests/generators`. Las propiedades les inyectan campos extra y organizaciones mezcladas.
   *
   * Las fugas se detectan buscando valores en el árbol renderizado, y solo se buscan los que
   * tienen 8 o más letras y dígitos (más cortos darían falsos positivos por azar). Un valor más
   * corto que se filtre (un estado, un código breve) no se detecta: el generador debe producir
   * en cada registro al menos un campo con valor largo (UUID, código o texto de 8 o más
   * caracteres) para que una fuga de ese registro se vea.
   */
  readonly data: fc.Arbitrary<Record<string, unknown>>;
  /** Pinta la pantalla (o el componente) con los datos recibidos. */
  readonly render: (data: Record<string, unknown>, context: ViewRenderContext) => ReactElement;
  /**
   * Nombres de campo de la vista que la pantalla puede pintar, a cualquier profundidad. El valor
   * de cualquier otro campo (inyectado o no) no debe aparecer en el árbol renderizado.
   */
  readonly allowedFields: readonly string[];
  /**
   * Campo que lleva la organización de cada registro (normalmente `organization_id`). Si existe,
   * PR-APP-10 mezcla organizaciones en todos los objetos que lo tienen; si no, la vista no
   * muestra registros de varias organizaciones y PR-APP-10 no la recorre.
   */
  readonly organizationOf?: string;
}

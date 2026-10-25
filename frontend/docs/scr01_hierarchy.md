# SCR-01: región de alta y configuración del lazo

`VITE_PLANT_DATA_REGION` es un **dato público de despliegue**, no una credencial. La construcción
de la SPA recibe su valor explícito para `POST /plants`; no hay valor por defecto, selección en la
interfaz, inferencia por país ni copia de otra planta. La organización siempre sale de la sesión.
El valor configurado se muestra como texto; nunca aparece como campo editable u oculto.

Si falta o no cumple la longitud del esquema generado de `PlantBody.data_region`, el formulario
informa la falta de configuración y no permite enviar el alta. Esto también permite administrar
plantas existentes sin inventar la residencia de la primera. El servidor sigue siendo la autoridad
de autorización y residencia; modificar la SPA no elude sus validaciones.

Los rangos de SUP-U05-39 se publican como metadatos JSON Schema derivados de
`loop.config.parameters.INTEGER_PARAMETERS` y de los límites de motivo ya existentes. No se
duplican valores en TypeScript ni se trasladan los rechazos del dominio al validador Pydantic:
el servidor conserva `400 invalid_request` con `loop_deadline_out_of_range` o
`loop_configuration_change_reason_required` (A-68). El cliente usa ese esquema generado para
anclar errores estructurales antes de enviar y presenta el rechazo del servidor si lo hay.
Las relaciones entre parámetros y la validez de fechas declaradas las decide el servidor.

La zona horaria del lazo se hereda de `Plant.timezone` y se muestra como dato informativo;
SUP-U05-39 no acepta cambiarla. Los cambios no mueven plazos ni revisiones ya fijados.

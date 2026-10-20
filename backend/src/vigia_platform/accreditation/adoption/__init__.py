"""``accreditation.adoption`` (LC-LAZ-29; C-PLA-31 y 33; TASK-323): la adopción del lazo por zona.

- ``counter``: el contador de actividad por zona y día (``AdoptionCounter``, DE §3.6) que las
  escrituras del lazo alimentan en su propia transacción, **sin ningún dato de usuario**
  (BR-LAZ-134 y 135, H-33, P3);
- ``streak``: la racha de silencio de una zona activa, función pura (BR-LAZ-136);
- ``task``: la tarea ``adoption_check`` que publica ``adoption_low`` una sola vez por racha;
- ``reads``: la consulta de SUP-U05-32 (``GET /accreditation/adoption``).

El paquete no importa nada al cargarse: ``loop.projection.store`` usa ``counter`` y no debe
arrastrar la tarea ni sus puertos.
"""

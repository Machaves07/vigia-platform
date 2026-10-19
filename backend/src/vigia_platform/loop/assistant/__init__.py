"""El asistente de causa raíz (LC-LAZ-13 y 15; C-PLA-27; BR-LAZ-67 a 76; TASK-316).

Propone un borrador que cita evidencia; nunca decide, nunca firma y nunca ve datos de personas:

- ``port``: ``AssistantProviderPort``, lo único que habla con el proveedor (el adaptador real es
  de TASK-344; aquí, ``loop.adapters.assistant.fake.FakeAssistantProvider``);
- ``schema``: la salida estricta que el proveedor debe devolver;
- ``context``: la frontera cerrada de lo que sale (BR-LAZ-67 y 68) con alias por sesión;
- ``budget``: los límites exactos y el tope mensual de fichas (PAT-LAZ-ESC-01 y 02);
- ``service``: SUP-U05-19 y 20 en ``vigia-api``;
- ``consumer``: ``assistant_worker`` en ``vigia-worker``.
"""

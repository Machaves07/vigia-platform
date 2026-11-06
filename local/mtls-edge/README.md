# `vigia-mtls-edge`: balanceador del entorno de integración

**Es una pieza de integración, no de producto** (TASK-542, adenda A-76 D3). En producción termina TLS el ALB de AWS (A-47). Este proceso hace lo mismo en `server-casa`: la aplicación no cambia y lee las mismas cabeceras `X-Amzn-Mtls-Clientcert-*`.

| Escucha | Puerto | Certificado de cliente | `X-Amzn-Mtls-*` que llegan a la aplicación |
|---|---|---|---|
| `app.` | 8443 | no se pide | ninguna: las entrantes se descartan |
| `nodes.` | 8444 | se verifica en el saludo TLS contra `vigia-edge/ca/root.pem` | las cinco de la hoja verificada, una vez cada una; las entrantes se descartan |

Sin certificado, `nodes.` deja pasar la petición sin cabeceras y la aplicación responde `node_not_enrolled`, como el balanceador de pruebas. Un certificado de otra autoridad no completa el saludo.

## Certificado de servidor

Se genera **una vez** y persiste en un volumen. Repetir la orden no lo cambia:

```bash
vigia-mtls-edge init-cert --san app.<zona> --san nodes.<zona> --ip <ip>
```

Exige un nombre `app.`, uno `nodes.` y al menos una IP. Escribe la clave (`0600`), el certificado y la raíz `edge-ca.pem`, que se reparte a navegadores y nodos. La clave de esa raíz no se guarda: para renovar hay que borrar el trío, repetir la orden y volver a repartir la raíz.

## Variables

| Variable | Obligatoria | Qué es |
|---|---|---|
| `VIGIA_EDGE_BIND` | sí | IP de la LAN o de la tailnet, o nombre de interfaz (`tailscale0`) |
| `VIGIA_EDGE_BACKENDS` | sí | `http://api-1:8000,http://api-2:8000`; reparto alterno por petición |
| `VIGIA_EDGE_SERVER_CERT`, `VIGIA_EDGE_SERVER_KEY` | sí | certificado y clave de `init-cert` |
| `VIGIA_EDGE_SERVER_CA` | no | la raíz exportada; por omisión, `edge-ca.pem` junto al certificado |
| `VIGIA_EDGE_CLIENT_CA` | sí | ruta o URL `http(s)` de `vigia-edge/ca/root.pem` |
| `VIGIA_EDGE_APP_PORT`, `VIGIA_EDGE_NODES_PORT` | no | 8443 y 8444 |
| `VIGIA_EDGE_RELOAD_SECONDS` | no | intervalo de recarga de la raíz; 60 |

## Raíz de los nodos

Se lee al arrancar y cada 60 s. Acepta una raíz o el paquete de dos raíces de una rotación (D-6). Si la lectura falla, conserva la raíz que tenía. Por ejemplo, tras reiniciar LocalStack comunitario, que pierde `ca/root.pem` hasta el siguiente `bootstrap`. Si aún no hay ninguna raíz válida, `nodes.` no admite ningún saludo y `app.` sigue atendiendo.

## Registro

Una línea JSON por intercambio en la salida de errores: escucha, método, ruta sin consulta, proceso, estado, si traía certificado y los nombres de las cabeceras descartadas. Nunca cuerpos ni cabeceras de autorización.

## Límites

- No comprueba la lista de revocación: la revocación la decide la aplicación por credencial.
- Mantiene cada petición y cada respuesta en memoria mientras las reenvía, sin tope propio.
- La composición del entorno es de TASK-545; el despliegue en `server-casa`, de TASK-546.

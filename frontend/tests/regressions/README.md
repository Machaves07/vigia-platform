# Regresiones de propiedades (PBT-10)

Cada contraejemplo reducido que encuentre fast-check se guarda aquí como prueba permanente, en
un archivo `<propiedad>.test.ts` por propiedad (`PR-APP-nn`), con:

- el contraejemplo reducido tal como lo imprimió fast-check, como datos literales;
- la semilla y el camino (`seed` y `path`) de la corrida que lo encontró, en un comentario;
- la propiedad que lo reproduce, ejecutada sobre ese ejemplo con `fc.assert(..., { examples })`.

Ningún dato real: solo los valores generados.

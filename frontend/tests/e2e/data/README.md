# Adjunto sintético

`guard.png` es un dibujo RGB de 64 × 64 píxeles: fondo blanco, rectángulo gris y
líneas blancas que representan un resguardo. No procede de cámaras ni de registros
históricos, no contiene personas, texto, EXIF ni coordenadas. Solo lleva los bloques
PNG `IHDR`, `IDAT` e `IEND`.

Se generó con Pillow creando una imagen blanca, dibujando un rectángulo
`(12, 8, 52, 56)` de color `(60, 80, 100)` y líneas verticales blancas de dos
píxeles en `x = 18, 26, 34, 42, 50`, entre `y = 12` y `y = 52`.

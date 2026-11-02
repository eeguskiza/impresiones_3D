# Presupuesto 3D

App web para presupuestar impresiones 3D a partir de un `.3mf` de Bambu Studio (laminado o no) y exportar el presupuesto a PDF. Funciona en local (WSL/Ubuntu) y en Vercel.

- Lee el `.3mf` (laminado o no): pieza, imagen, impresora, filamentos usados y su precio por kilo.
- Calcula el precio en vivo: material, máquina (luz, amortización, mantenimiento y fallos), complejidad de la pieza, tu tiempo y margen con redondeo.
- Presupuesto de una pieza o pedido combo de hasta 20 piezas.
- PDF de una página con todos los conceptos y su importe en una sola tabla que termina en el total.
- Historial con numeración correlativa y marca de pagado.
- Ajustes: precio por kilo de cada tipo de filamento (PLA, PETG…), guardado en la base de datos. Los valores por defecto están en `[filamentos_eur_kg]` de `config.toml`; para añadir un tipo nuevo basta con una línea ahí.

## Local (Ubuntu / WSL2)

WeasyPrint necesita Pango del sistema:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0
```

En Ubuntu 24.04 o superior añade también `libharfbuzz-subset0` (en 22.04 no existe; WeasyPrint funciona igual y solo muestra un aviso).

Python 3.11 o superior. Arranque:

```bash
./run.sh
```

La primera vez crea `.venv` e instala `requirements.txt` (con `uv` si está; si no, `venv` + `pip`). Escucha solo en `127.0.0.1` (si el puerto está ocupado prueba los siguientes) y abre el navegador de Windows. En local el PDF lo genera WeasyPrint, se descarga y se guarda en `presupuestos/`; el historial va en `registro.db` (SQLite).

Tests:

```bash
uv pip install -p .venv -r requirements-dev.txt   # o .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Los tests de lectura usan `ejemplos/Spiderman_urban.3mf`, que no está en el repo por su licencia: si falta, se saltan.

## Presupuesto único o pedido combo

Al empezar eliges el tipo:

- **Presupuesto único**: una pieza, un presupuesto.
- **Pedido combo**: varias piezas (cada una de su `.3mf`) en un solo presupuesto. Cargas la primera, ajustas sus filamentos y con «Añadir otra pieza» (o arrastrando el siguiente fichero) la pieza se guarda como un JSON ligero con sus datos y su coste, y el fichero se descarta. El total se calcula en el servidor sumando material, máquina y complejidad de todas las piezas y aplicando una sola vez tu tiempo de preparación, el margen y el redondeo. El PDF lista las piezas y agrupa los filamentos.

Pulsar «Nuevo» con un presupuesto abierto vuelve a la pantalla inicial.

## Vercel

1. En Vercel: **Add New → Project** e importa este repositorio (no hace falta tocar la configuración de build, va en `vercel.json`).
2. **Storage → Create Database → Neon (Postgres)** y conéctala al proyecto. Crea `DATABASE_URL`, que se usa para el historial y la numeración.
3. **Settings → Environment Variables**:
   - `EMISOR_NOMBRE`: tu nombre para el presupuesto.
   - `EMISOR_BIZUM`: tu teléfono de Bizum.
   - `APP_CLAVE`: contraseña de acceso (muy recomendable: sin ella cualquiera con la URL ve tu historial). El navegador la pide al entrar; el usuario da igual.
4. Vuelve a desplegar para que coja las variables.

Diferencias con local:

- Vercel no tiene las librerías de WeasyPrint, así que «Exportar PDF» registra el número y abre el diálogo de impresión con el mismo documento de la vista previa: elige **Guardar como PDF** (en Chrome/Edge el nombre del fichero ya viene puesto).
- Sin `DATABASE_URL` funciona, pero el historial se pierde en cada despliegue.
- CSS, JS y fuentes salen de la CDN de Vercel (`public/`); la página principal y la API pasan por la función, así que `APP_CLAVE` las protege.

### Qué pasa con el fichero

1. El navegador lee el `.3mf` (laminado o no) y extrae solo lo necesario: ajustes del proyecto, datos del laminado, imágenes de las placas y si la pieza está pintada. Las mallas y el G-code no se suben (Vercel limita las peticiones a 4,5 MB).
2. El servidor lo procesa en memoria, devuelve un resumen ligero (unos 45 KB) y lo descarta al terminar la petición: no se guarda en disco ni en ninguna base de datos.
3. Ese resumen viaja con cada cálculo; al cargar otro fichero o cerrar la pestaña desaparece. En la base de datos solo queda el registro del presupuesto exportado (número, cliente, modelo, horas, gramos y precio).

## Estructura

```
app/core.py          lectura del 3MF, filamentos, complejidad, cálculo, historial (SQLite o Postgres)
app/documento.py     contexto de la plantilla, HTML y PDF (WeasyPrint)
app/main.py          API FastAPI (sin estado: el análisis viaja con cada petición)
app/__main__.py      arranque local de uvicorn y apertura del navegador
app/index.html       página de la interfaz
api/index.py         entrada para Vercel
templates/           presupuesto.html: plantilla única para vista previa y PDF
public/static/       interfaz (app.css, app.js)
public/fonts/        Poppins (OFL)
tests/               pytest
```

## Configuración

`config.toml` está en el repo, así que **no pongas ahí datos personales**: en local van en `config.local.toml` (no se sube; sobrescribe lo que pongas) y en Vercel en variables de entorno (`EMISOR_NOMBRE`, `EMISOR_BIZUM`, `MARCA_NOMBRE`).

```toml
# config.local.toml
[emisor]
nombre = "Tu nombre"
bizum = "600000000"
```

- `[marca]`: nombre, `logo` opcional (ruta a SVG/PNG; vacío = solo texto), `lema`, colores.
- `[app]`: `puerto`, `nivel_log` y `carpeta_salida` (solo local).
- `[maquina]`, `[energia]`, `[produccion]`: costes, tu hora, margen y redondeo.
- `[complejidad]`: ajuste sobre material y máquina según la pieza (un color, colores extra, pintada a color, soportes, impresión larga) con sus límites.
- `[filamentos_eur_kg]`: precio por perfil, con o sin impresora (`"Bambu PLA Matte" = 23.57` o `"Bambu PLA Matte @BBL X2D" = 24.99`), o por tipo (`PLA = 20`). Si falta, se usa el de Bambu Studio; se puede ajustar en la interfaz. Viene cargado `PLA = 19.99` (cualquier PLA, de Bambu o genérico) y PETG Translucent a 24,99 €/kg. El filamento se detecta del perfil elegido al laminar, y bajo cada uno la interfaz indica de dónde sale su €/kg (tu lista, genérico por tipo o Bambu Studio).

La interfaz avisa mientras quede algún `[REEMPLAZAR]`.

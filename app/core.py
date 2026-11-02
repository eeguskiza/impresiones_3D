import base64
import json
import math
import os
import re
import sqlite3
import tomllib
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops

MAX_DESCOMPRIMIDO = 500 * 1024 * 1024
NS_3MF = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PALETA = {
    "Blanco": (255, 255, 255), "Negro": (0, 0, 0), "Gris": (128, 128, 128),
    "Rojo": (200, 30, 30), "Naranja": (240, 130, 30), "Amarillo": (240, 220, 40),
    "Verde": (40, 160, 60), "Azul": (30, 80, 200), "Morado": (120, 50, 160),
    "Rosa": (240, 130, 180), "Marrón": (120, 70, 30), "Beige": (220, 200, 160),
}
VARIABLES = {"EMISOR_NOMBRE": ("emisor", "nombre"), "EMISOR_BIZUM": ("emisor", "bizum"),
             "MARCA_NOMBRE": ("marca", "nombre")}
TABLA = ("CREATE TABLE IF NOT EXISTS presupuestos (id INTEGER PRIMARY KEY, fecha TEXT, cliente TEXT, modelo TEXT, "
         "fichero TEXT, horas DOUBLE PRECISION, gramos DOUBLE PRECISION, coste DOUBLE PRECISION, "
         "precio DOUBLE PRECISION, pagado INTEGER DEFAULT 0)")


class ErrorPresupuesto(Exception):
    pass


@dataclass
class Filamento:
    id: int
    tipo: str
    perfil: str
    color: str
    gramos: float
    eur_kg: float | None
    origen: str = ""


@dataclass
class Placa:
    indice: int
    segundos: float
    filamentos: list
    imagen: bytes | None


@dataclass
class Desglose:
    horas: float
    material: float
    maquina: float
    mano_obra: float
    coste: float
    precio: float
    ajuste: float = 0.0
    ajuste_pct: float = 0.0
    nivel: str = "Normal"
    motivos: list = field(default_factory=list)


@dataclass
class Proyecto:
    meta: dict
    filamentos: dict
    placas: list
    imagen: bytes | None
    avisos: list = field(default_factory=list)
    soportes: bool = False
    pintado: bool = False

    @property
    def laminado(self):
        return bool(self.placas)

    @property
    def impresora(self):
        return self.meta.get("_impresora", "")


def fusionar(base, extra):
    return {k: fusionar(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
            for k, v in {**base, **extra}.items()}


def cargar_config(ruta):
    ruta = Path(ruta)
    cfg = tomllib.loads(ruta.read_text(encoding="utf-8"))
    local = ruta.with_name(f"{ruta.stem}.local.toml")
    if local.is_file():
        cfg = fusionar(cfg, tomllib.loads(local.read_text(encoding="utf-8")))
    for variable, (seccion, clave) in VARIABLES.items():
        if os.environ.get(variable):
            cfg.setdefault(seccion, {})[clave] = os.environ[variable]
    return cfg


def pendientes(cfg, ruta=""):
    for k, v in cfg.items():
        if isinstance(v, dict):
            yield from pendientes(v, f"{ruta}{k}.")
        elif isinstance(v, str) and "[REEMPLAZAR]" in v:
            yield ruta + k


def nombre_color(hexa):
    h = hexa.lstrip("#")[:6]
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return min(PALETA, key=lambda n: sum((a - b) ** 2 for a, b in zip(PALETA[n], rgb, strict=True)))


def perfil_corto(perfil):
    return re.sub(r"\s*@.*$", "", perfil)


def eur(x):
    return f"{x:.2f}".replace(".", ",") + " €"


def gramos_txt(g):
    return f"{g:.1f} g".replace(".", ",")


def duracion_txt(horas):
    h, m = divmod(round(horas * 60), 60)
    return f"{h} h {m:02d} min"


def metadatos_modelo(z):
    meta = {}
    with z.open("3D/3dmodel.model") as f:
        for evento, el in ET.iterparse(f, events=("start", "end")):
            if evento == "start" and el.tag == NS_3MF + "resources":
                break
            if evento == "end" and el.tag == NS_3MF + "metadata":
                meta[el.get("name")] = (el.text or "").strip()
    return meta


def filamentos_proyecto(ajustes, cfg):
    precios = cfg.get("filamentos_eur_kg", {})
    campo = lambda k, i, defecto: ajustes.get(k, [])[i] if i < len(ajustes.get(k, [])) else defecto
    res = {}
    for i, color in enumerate(ajustes.get("filament_colour", [])):
        tipo = campo("filament_type", i, "?")
        perfil = campo("filament_settings_id", i, tipo)
        coste = campo("filament_cost", i, None)
        origen, eur_kg = next(((o, precios[k]) for o, k in (("lista", perfil), ("lista", perfil_corto(perfil)), ("tipo", tipo))
                               if k in precios), ("bambu", float(coste)) if coste else ("", None))
        res[i + 1] = Filamento(i + 1, tipo, perfil, color, 0.0, None if eur_kg is None else float(eur_kg), origen)
    return res


def recortar(datos):
    im = Image.open(BytesIO(datos)).convert("RGBA")
    fondo = Image.new("RGBA", im.size, (0, 0, 0, 0))
    fondo.alpha_composite(im)
    blanco = Image.new("RGB", im.size, "white")
    blanco.paste(im, mask=im.getchannel("A"))
    caja = ImageChops.difference(blanco, Image.new("RGB", im.size, "white")).getbbox()
    if caja:
        fondo = fondo.crop(caja)
    lado = int(max(fondo.size) * 1.06)
    lienzo = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    lienzo.paste(fondo, ((lado - fondo.width) // 2, (lado - fondo.height) // 2))
    salida = BytesIO()
    lienzo.save(salida, "PNG")
    return salida.getvalue()


def miniatura(datos, lado=200):
    im = Image.open(BytesIO(datos))
    if max(im.size) <= lado:
        return datos
    im.thumbnail((lado, lado))
    salida = BytesIO()
    im.save(salida, "PNG", optimize=True)
    return salida.getvalue()


def imagen_placa(z, indice):
    nombres = set(z.namelist())
    for n in (f"Metadata/plate_{indice}.png", "Auxiliaries/.thumbnails/thumbnail_middle.png"):
        if n in nombres:
            return recortar(z.read(n))
    return None


def es_pintado(z):
    for n in z.namelist():
        if n.startswith("3D/") and n.endswith(".model"):
            with z.open(n) as f:
                previo = b""
                while trozo := f.read(1 << 20):
                    bloque = previo[-32:] + trozo
                    if b"paint_color" in bloque or b"mmu_segmentation" in bloque:
                        return True
                    previo = trozo
    return False


def placas_laminadas(z, fils):
    if "Metadata/slice_info.config" not in z.namelist():
        return []
    placas = []
    for p in ET.fromstring(z.read("Metadata/slice_info.config")).iter("plate"):
        m = {e.get("key"): e.get("value") for e in p.findall("metadata")}
        indice = int(m.get("index", len(placas) + 1))
        usados = []
        for f in p.findall("filament"):
            fid = int(f.get("id"))
            base = fils.get(fid)
            if base is None:
                raise ErrorPresupuesto(f"El filamento {fid} del laminado no está en el proyecto.")
            usados.append(Filamento(fid, f.get("type") or base.tipo, base.perfil,
                                    f.get("color") or base.color, float(f.get("used_g", 0)), base.eur_kg, base.origen))
        placas.append(Placa(indice, float(m.get("prediction", 0)), usados, imagen_placa(z, indice)))
    return placas


def leer_3mf(datos, cfg, pintado=None):
    try:
        z = zipfile.ZipFile(BytesIO(datos))
    except zipfile.BadZipFile:
        raise ErrorPresupuesto("El fichero no es un 3MF válido.") from None
    with z:
        nombres = z.namelist()
        if sum(i.file_size for i in z.infolist()) > MAX_DESCOMPRIMIDO:
            raise ErrorPresupuesto("El fichero descomprimido es demasiado grande.")
        if "Metadata/project_settings.config" not in nombres:
            raise ErrorPresupuesto("El 3MF no tiene project_settings: guárdalo como proyecto desde Bambu Studio.")
        try:
            ajustes = json.loads(z.read("Metadata/project_settings.config"))
            meta = metadatos_modelo(z) if "3D/3dmodel.model" in nombres else {}
            meta["_impresora"] = ajustes.get("printer_model", "")
            fils = filamentos_proyecto(ajustes, cfg)
            placas = placas_laminadas(z, fils)
        except (ValueError, ET.ParseError):
            raise ErrorPresupuesto("El 3MF está dañado: no se pueden leer sus ajustes.") from None
        imagen = next((p.imagen for p in placas if p.imagen), None) if placas else imagen_placa(z, 1)
        if pintado is None:
            pintado = es_pintado(z)
    avisos = []
    if "X2D" not in meta["_impresora"]:
        avisos.append(["aviso", f"Para {meta['_impresora'] or 'otra impresora'}",
                       f"El proyecto está configurado para {meta['_impresora'] or 'otra impresora'}, "
                       "no para la X2D. Lamina para la X2D antes de presupuestar."])
    if meta.get("License"):
        avisos.append(["info", meta["License"], f"Licencia del modelo: {meta['License']}"])
    if not placas:
        avisos.append(["aviso", "Sin laminar",
                       "El fichero no está laminado. Introduce el tiempo y los gramos de la vista previa de Bambu Studio."])
    return Proyecto(meta, fils, placas, imagen, avisos, ajustes.get("enable_support") == "1", pintado)


def placas_manual(proyecto, segundos, gramos):
    usados = [replace(f, gramos=float(gramos.get(f.id, 0))) for f in proyecto.filamentos.values()
              if gramos.get(f.id, 0) > 0]
    return [Placa(1, segundos, usados, proyecto.imagen)]


def evaluar_complejidad(proyecto, placas, cfg):
    c = cfg.get("complejidad", {})
    colores = len({f.id for pl in placas for f in pl.filamentos if f.gramos > 0})
    horas = sum(pl.segundos for pl in placas) / 3600
    motivos = []
    if colores == 1 and c.get("un_color"):
        motivos.append(("Un solo color", "sin cambios de filamento ni purga", c["un_color"]))
    elif colores > 2 and c.get("por_color_extra"):
        motivos.append((f"{colores} colores", "más cambios de filamento y más purga", (colores - 2) * c["por_color_extra"]))
    if proyecto.pintado and colores > 1 and c.get("pintado"):
        motivos.append(("Pintada a color", "cambia de filamento en muchas capas", c["pintado"]))
    if proyecto.soportes and c.get("soportes"):
        motivos.append(("Soportes", "más riesgo de fallo y más limpieza", c["soportes"]))
    if horas > c.get("horas_larga", 12) and c.get("larga"):
        motivos.append((f"Más de {c.get('horas_larga', 12):g} h", "más tiempo expuesta a fallos", c["larga"]))
    pct = sum(m[2] for m in motivos)
    pct = round(min(max(pct, c.get("minimo", pct)), c.get("maximo", pct)), 4)
    nivel = "Sencilla" if pct < 0 else "Normal" if pct == 0 else "Algo compleja" if pct <= 0.06 else "Compleja"
    return pct, nivel, motivos


def calcular(placas, cfg, minutos, ajuste_pct=0.0):
    m, e, p = cfg["maquina"], cfg["energia"], cfg["produccion"]
    horas = sum(pl.segundos for pl in placas) / 3600
    material = sum(f.gramos * f.eur_kg / 1000 for pl in placas for f in pl.filamentos)
    uso = horas * (m["potencia_media_w"] / 1000 * e["precio_kwh"]
                   + m["precio_eur"] / m["horas_amortizacion"] + m["mantenimiento_eur_h"])
    directo = (material + uso) / (1 - p["tasa_fallo"])
    return cerrar(horas, material, directo - material, directo * ajuste_pct, ajuste_pct, cfg, minutos)


def cerrar(horas, material, maquina, ajuste, ajuste_pct, cfg, minutos):
    p = cfg["produccion"]
    mano_obra = minutos / 60 * p["mano_obra_eur_h"]
    coste = material + maquina + ajuste + mano_obra
    r = p["redondeo_eur"]
    bruto = coste * (1 + p["margen"])
    precio = math.ceil(bruto / r - 1e-9) * r if r > 0 else round(bruto, 2)
    return Desglose(horas, material, maquina, mano_obra, coste, precio, ajuste, ajuste_pct)


def calcular_combo(piezas, cfg, minutos, margen=None):
    if not piezas:
        raise ErrorPresupuesto("El pedido no tiene piezas.")
    if margen is not None:
        cfg = {**cfg, "produccion": {**cfg["produccion"], "margen": margen}}
    suma = lambda k: sum(p[k] for p in piezas)
    return cerrar(suma("horas"), suma("material"), suma("maquina"), suma("ajuste"), 0.0, cfg, minutos)


def presupuestar(proyecto, cfg, minutos, margen=None, eur_kg=None, segundos=None, gramos=None, complejidad=True):
    if proyecto.laminado:
        placas = proyecto.placas
    else:
        if not segundos or segundos <= 0:
            raise ErrorPresupuesto("Introduce el tiempo de impresión.")
        if not any(g > 0 for g in (gramos or {}).values()):
            raise ErrorPresupuesto("Introduce los gramos de al menos un filamento.")
        placas = placas_manual(proyecto, segundos, gramos)
    precios = eur_kg or {}
    placas = [replace(pl, filamentos=[replace(f, eur_kg=precios.get(f.id, f.eur_kg)) for f in pl.filamentos])
              for pl in placas]
    for f in (f for pl in placas for f in pl.filamentos):
        if f.eur_kg is None or f.eur_kg < 0:
            raise ErrorPresupuesto(f"Sin precio para el filamento {f.id} ({perfil_corto(f.perfil)}): indica su €/kg.")
    if margen is not None:
        cfg = {**cfg, "produccion": {**cfg["produccion"], "margen": margen}}
    pct, nivel, motivos = evaluar_complejidad(proyecto, placas, cfg)
    d = calcular(placas, cfg, minutos, pct if complejidad else 0.0)
    return placas, replace(d, ajuste_pct=pct, nivel=nivel, motivos=motivos)


def reparto(d):
    directo = d.material + d.maquina
    rebaja = min(d.ajuste, 0) / directo if directo else 0
    partes = [("material", "Material", d.material * (1 + rebaja)), ("maquina", "Máquina y luz", d.maquina * (1 + rebaja)),
              ("complejidad", "Complejidad", max(d.ajuste, 0)), ("tiempo", "Tu tiempo", d.mano_obra),
              ("margen", "Margen y redondeo", d.precio - d.coste)]
    return [(clave, nombre, importe) for clave, nombre, importe in partes if importe > 0.005]


def agregar_combo(piezas):
    filas = {}
    for p in piezas:
        for f in p["filamentos"]:
            a = filas.setdefault((perfil_corto(f["perfil"]), f["color"][:7].upper()),
                                 {"perfil": f["perfil"], "color": f["color"], "gramos": 0.0, "coste": 0.0})
            a["gramos"] += f["gramos"]
            a["coste"] += f["gramos"] * f["eur_kg"] / 1000
    return list(filas.values())


def serializar(proyecto):
    d = asdict(proyecto)
    d["imagen"] = base64.b64encode(proyecto.imagen).decode() if proyecto.imagen else None
    for pl, original in zip(d["placas"], proyecto.placas, strict=True):
        pl["imagen"] = base64.b64encode(original.imagen).decode() if original.imagen else None
    return base64.urlsafe_b64encode(zlib.compress(json.dumps(d).encode())).decode()


def deserializar(texto):
    try:
        d = json.loads(zlib.decompress(base64.urlsafe_b64decode(texto)))
        imagen = lambda b: base64.b64decode(b) if b else None
        return Proyecto(
            d["meta"], {int(k): Filamento(**f) for k, f in d["filamentos"].items()},
            [Placa(pl["indice"], pl["segundos"], [Filamento(**f) for f in pl["filamentos"]], imagen(pl["imagen"]))
             for pl in d["placas"]],
            imagen(d["imagen"]), d["avisos"], d["soportes"], d["pintado"])
    except (ValueError, KeyError, TypeError, zlib.error):
        raise ErrorPresupuesto("El análisis no es válido: vuelve a cargar el fichero.") from None


def agregar(placas):
    agregados = {}
    for pl in placas:
        for f in pl.filamentos:
            a = agregados.setdefault(f.id, replace(f, gramos=0.0))
            a.gramos += f.gramos
    return list(agregados.values())


_TABLAS_CREADAS = set()


@contextmanager
def _db(destino):
    postgres = isinstance(destino, str) and destino.startswith(("postgres://", "postgresql://"))
    if postgres:
        import psycopg
        from psycopg.rows import dict_row
        db = psycopg.connect(destino, row_factory=dict_row)
    else:
        db = sqlite3.connect(destino)
        db.row_factory = sqlite3.Row
    try:
        def ejecutar(sql, args=()):
            return db.execute(sql.replace("?", "%s") if postgres else sql, args)

        ejecutar.postgres = postgres
        if str(destino) not in _TABLAS_CREADAS:
            if postgres:
                ejecutar("SELECT pg_advisory_xact_lock(3203)")
            ejecutar(TABLA)
            _TABLAS_CREADAS.add(str(destino))
        yield ejecutar
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def numero(id_, fecha=None):
    return f"{(fecha or date.today()).year}-{id_:04d}"


def siguiente_numero(destino):
    with _db(destino) as ejecutar:
        return numero(ejecutar("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM presupuestos").fetchone()["n"])


def registrar(destino, cliente, modelo, fichero, d, gramos, generar=None):
    with _db(destino) as ejecutar:
        if ejecutar.postgres:
            ejecutar("SELECT pg_advisory_xact_lock(3203)")
        id_ = ejecutar("INSERT INTO presupuestos (id, fecha, cliente, modelo, fichero, horas, gramos, coste, precio) "
                       "VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM presupuestos), ?, ?, ?, ?, ?, ?, ?, ?) "
                       "RETURNING id",
                       (date.today().isoformat(), cliente, modelo, fichero, d.horas, gramos, d.coste, d.precio)
                       ).fetchone()["id"]
        num = numero(id_)
        return id_, num, generar(num) if generar else None


def historial(destino):
    with _db(destino) as ejecutar:
        filas = ejecutar("SELECT * FROM presupuestos ORDER BY id DESC").fetchall()
    return [{**dict(f), "numero": numero(f["id"], date.fromisoformat(f["fecha"])), "pagado": bool(f["pagado"])}
            for f in filas]


def marcar_pagado(destino, id_, pagado):
    with _db(destino) as ejecutar:
        return ejecutar("UPDATE presupuestos SET pagado = ? WHERE id = ?", (int(pagado), id_)).rowcount > 0

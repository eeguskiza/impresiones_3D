import asyncio
import base64
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import core, documento

BASE = documento.BASE
MAX_SUBIDA = 100 * 1024 * 1024
log = logging.getLogger("presupuesto3d")


class PiezaDatos(BaseModel):
    proyecto: str
    eur_kg: dict[int, float] = {}
    tiempo_min: float | None = Field(default=None, ge=0)
    gramos: dict[int, float] = {}
    complejidad: bool = True


class Datos(PiezaDatos):
    cliente: str = ""
    minutos: float = Field(ge=0)
    margen: float = Field(ge=0)
    desglose: bool = True


class FilamentoPieza(BaseModel):
    perfil: str
    color: str
    gramos: float = Field(ge=0)
    eur_kg: float = Field(ge=0)


class Pieza(BaseModel):
    titulo: str
    fichero: str
    imagen: str | None = None
    horas: float = Field(ge=0)
    gramos: float = Field(ge=0)
    material: float
    maquina: float
    ajuste: float
    ajuste_pct: float = 0.0
    motivos: list[str] = []
    filamentos: list[FilamentoPieza]


class Combo(BaseModel):
    piezas: list[Pieza] = Field(default=[], max_length=19)
    actual: PiezaDatos | None = None
    cliente: str = ""
    minutos: float = Field(ge=0)
    margen: float = Field(ge=0)
    desglose: bool = True


class Pagado(BaseModel):
    pagado: bool


class Ajustes(BaseModel):
    filamentos_eur_kg: dict[str, float] = Field(max_length=50)


def destino_por_defecto():
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if url:
        return url
    if os.environ.get("VERCEL"):
        log.warning("Sin DATABASE_URL: el historial se guarda en /tmp y se perderá.")
        return Path("/tmp/registro.db")
    return BASE / "registro.db"


def nombre_base(fichero):
    return re.sub(r"\.gcode$", "", Path(fichero).stem, flags=re.I)


def totales(d, filamentos):
    return {
        "horas": d.horas, "material": d.material, "maquina": d.maquina, "mano_obra": d.mano_obra,
        "coste": d.coste, "precio": d.precio, "margen_redondeo": d.precio - d.coste, "ajuste": d.ajuste,
        "reparto": [{"clave": k, "nombre": n, "importe": v} for k, n, v in core.reparto(d)],
        "gramos": sum(f["gramos"] for f in filamentos),
        "filamentos": [{"perfil": core.perfil_corto(f["perfil"]), "color": f["color"][:7],
                        "nombre_color": core.nombre_color(f["color"]), "gramos": f["gramos"], "coste": f["coste"]}
                       for f in filamentos],
    }


def complejidad(d, aplicado):
    return {"ajuste_pct": d.ajuste_pct, "nivel": d.nivel, "aplicado": aplicado,
            "motivos": [{"titulo": t, "detalle": x, "pct": v} for t, x, v in d.motivos]}


def pieza_json(p, placas, d):
    imagen = next((pl.imagen for pl in placas if pl.imagen), p.imagen)
    fils = core.agregar(placas)
    return {
        "titulo": p.meta.get("Title") or nombre_base(p.meta.get("_fichero", "modelo.3mf")),
        "fichero": p.meta.get("_fichero", "modelo.3mf"),
        "imagen": base64.b64encode(core.miniatura(imagen)).decode() if imagen else None,
        "horas": d.horas, "gramos": sum(f.gramos for f in fils),
        "material": d.material, "maquina": d.maquina, "ajuste": d.ajuste, "ajuste_pct": d.ajuste_pct,
        "motivos": [m[0] for m in d.motivos],
        "filamentos": [{"perfil": f.perfil, "color": f.color, "gramos": f.gramos, "eur_kg": f.eur_kg} for f in fils],
    }


def crear_app(config=BASE / "config.toml", db=None):
    db = db or destino_por_defecto()
    clave = os.environ.get("APP_CLAVE")
    proximo = {"numero": None, "hasta": 0.0}
    precios = {"valores": None, "hasta": 0.0}

    def siguiente_numero():
        if proximo["numero"] is None or time.monotonic() > proximo["hasta"]:
            proximo.update(numero=core.siguiente_numero(db), hasta=time.monotonic() + 30)
        return proximo["numero"]

    def precios_guardados():
        if precios["valores"] is None or time.monotonic() > precios["hasta"]:
            precios.update(valores=core.precios_filamento(db), hasta=time.monotonic() + 30)
        return precios["valores"]

    def cfg():
        c = core.cargar_config(config)
        c["filamentos_eur_kg"] = {**c.get("filamentos_eur_kg", {}), **precios_guardados()}
        return c

    def construir(datos, minutos=0, margen=None):
        c = cfg()
        segundos = datos.tiempo_min * 60 if datos.tiempo_min is not None else None
        try:
            p = core.deserializar(datos.proyecto)
            placas, d = core.presupuestar(p, c, minutos, margen, datos.eur_kg, segundos, datos.gramos,
                                          datos.complejidad)
        except core.ErrorPresupuesto as e:
            raise HTTPException(422, str(e)) from None
        return p, c, placas, d

    def construir_unico(datos):
        return construir(datos, datos.minutos, datos.margen)

    def construir_combo(combo):
        c = cfg()
        piezas, actual, d_actual = [x.model_dump() for x in combo.piezas], None, None
        if combo.actual:
            p, _, placas, d_actual = construir(combo.actual)
            actual = pieza_json(p, placas, d_actual)
        try:
            d = core.calcular_combo(piezas + ([actual] if actual else []), c, combo.minutos, combo.margen)
        except core.ErrorPresupuesto as e:
            raise HTTPException(422, str(e)) from None
        return c, piezas + ([actual] if actual else []), actual, d_actual, d

    def contexto_unico(datos, p, c, placas, d, numero, titulo_pagina=""):
        return documento.contexto(c, p, placas, d, datos.cliente, numero, datos.desglose,
                                  nombre_base(p.meta.get("_fichero", "modelo.3mf")), titulo_pagina, datos.margen)

    def documento_final(c, d, cliente, modelo, fichero, gramos, base_nombre, crear_ctx, generar):
        def con_numero(numero):
            nombre = re.sub(r'[\\/:*?"<>|]+', "_", f"{c['emisor']['titulo_documento']}_{numero}_{base_nombre}")
            return nombre, generar(crear_ctx(numero, nombre), nombre, c)

        _, numero, (nombre, resultado) = core.registrar(db, cliente or "—", modelo, fichero, d, gramos, con_numero)
        proximo["numero"] = None
        log.info("Presupuesto %s: %s", numero, core.eur(d.precio))
        return numero, nombre, resultado

    def final_unico(datos, generar):
        p, c, placas, d = construir_unico(datos)
        fichero = p.meta.get("_fichero", "modelo.3mf")
        return documento_final(c, d, datos.cliente, p.meta.get("Title") or nombre_base(fichero), fichero,
                               sum(f.gramos for pl in placas for f in pl.filamentos), nombre_base(fichero),
                               lambda n, t: contexto_unico(datos, p, c, placas, d, n, t), generar)

    def final_combo(combo, generar):
        c, piezas, _, _, d = construir_combo(combo)
        return documento_final(c, d, combo.cliente, "Combo: " + ", ".join(p["titulo"] for p in piezas)[:200],
                               f"{len(piezas)} piezas", sum(p["gramos"] for p in piezas), "combo",
                               lambda n, t: documento.contexto_combo(c, piezas, d, combo.cliente, n, combo.desglose, t,
                                                                     combo.margen),
                               generar)

    def generar_pdf(ctx, nombre, c):
        pdf = documento.pdf(ctx)
        carpeta = Path(c.get("app", {}).get("carpeta_salida", "presupuestos")).expanduser()
        ruta = (carpeta if carpeta.is_absolute() else BASE / carpeta) / f"{nombre}.pdf"
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_bytes(pdf)
        except OSError as e:
            log.warning("No se pudo guardar %s: %s", ruta, e)
            ruta = None
        return pdf, ruta

    def respuesta_pdf(numero, nombre, pdf, ruta):
        cabeceras = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(nombre)}.pdf", "X-Numero": numero}
        if ruta:
            cabeceras["X-Ruta"] = quote(str(ruta))
        return Response(pdf, media_type="application/pdf", headers=cabeceras)

    def exigir_pdf():
        if not documento.pdf_disponible():
            raise HTTPException(501, "Este servidor no puede generar PDF: registra e imprime desde el navegador.")

    app = FastAPI(title="Presupuesto 3D")
    app.mount("/static", StaticFiles(directory=BASE / "public" / "static"), name="static")
    app.mount("/fonts", StaticFiles(directory=BASE / "public" / "fonts"), name="fonts")

    @app.middleware("http")
    async def acceso(request: Request, siguiente):
        if clave:
            usuario_clave = request.headers.get("authorization", "").removeprefix("Basic ")
            try:
                recibida = base64.b64decode(usuario_clave).decode().partition(":")[2]
            except ValueError:
                recibida = ""
            if not secrets.compare_digest(recibida.encode(), clave.encode()):
                return Response("Acceso restringido", 401, {"WWW-Authenticate": 'Basic realm="Presupuestos"'})
        return await siguiente(request)

    @app.get("/", include_in_schema=False)
    def inicio():
        return FileResponse(BASE / "app" / "index.html")

    @app.get("/api/config")
    def configuracion():
        c = cfg()
        return {"pendientes": list(core.pendientes(c)), "marca": c["marca"]["nombre"],
                "acento": c["marca"]["color_acento"], "margen": c["produccion"]["margen"],
                "minutos": c["produccion"]["minutos_por_defecto"], "pdf_servidor": documento.pdf_disponible(),
                "complejidad": {k: c.get("complejidad", {}).get(k, 0) for k in ("minimo", "maximo")}}

    @app.get("/api/ajustes")
    def ver_ajustes():
        return {"filamentos_eur_kg": cfg()["filamentos_eur_kg"]}

    @app.put("/api/ajustes")
    def guardar_ajustes(ajustes: Ajustes):
        nuevos = {k.strip(): v for k, v in ajustes.filamentos_eur_kg.items() if k.strip()}
        if any(not 0 < v < 1000 for v in nuevos.values()):
            raise HTTPException(422, "El precio por kilo debe estar entre 0 y 1000 €.")
        core.guardar_precios_filamento(db, nuevos)
        precios["valores"] = None
        return ver_ajustes()

    @app.post("/api/analizar")
    async def analizar(request: Request, fichero: str = "modelo.3mf", pintado: bool | None = None):
        if int(request.headers.get("content-length") or 0) > MAX_SUBIDA:
            raise HTTPException(413, "El fichero supera el límite de 100 MB.")
        datos = bytearray()
        async for trozo in request.stream():
            datos += trozo
            if len(datos) > MAX_SUBIDA:
                raise HTTPException(413, "El fichero supera el límite de 100 MB.")
        if not fichero.lower().endswith(".3mf"):
            raise HTTPException(422, "El fichero debe ser un .3mf de Bambu Studio.")
        try:
            p = await asyncio.to_thread(core.leer_3mf, bytes(datos), cfg(), pintado)
        except core.ErrorPresupuesto as e:
            raise HTTPException(422, str(e)) from None
        finally:
            del datos
        p.meta["_fichero"] = fichero
        log.info("Analizado %s (%s, laminado=%s)", fichero, p.impresora, p.laminado)
        gramos = {f.id: f.gramos for f in core.agregar(p.placas)}
        return {
            "proyecto": core.serializar(p), "fichero": fichero,
            "titulo": p.meta.get("Title") or Path(fichero).stem, "disenador": p.meta.get("Designer", ""),
            "licencia": p.meta.get("License", ""), "impresora": p.impresora, "laminado": p.laminado,
            "placas": [{"indice": pl.indice, "segundos": pl.segundos} for pl in p.placas],
            "filamentos": [{"id": f.id, "tipo": f.tipo, "perfil": core.perfil_corto(f.perfil), "color": f.color[:7],
                            "nombre_color": core.nombre_color(f.color), "gramos": gramos.get(f.id, 0.0),
                            "eur_kg": f.eur_kg, "origen": f.origen} for f in p.filamentos.values()
                           if not p.laminado or gramos.get(f.id, 0) > 0],
            "imagen": base64.b64encode(p.imagen).decode() if p.imagen else None,
            "avisos": [{"tipo": t, "corto": c, "texto": x} for t, c, x in p.avisos],
        }

    @app.post("/api/calcular")
    def calcular(datos: Datos, vista: bool = False):
        p, c, placas, d = construir_unico(datos)
        fils = [{"perfil": f.perfil, "color": f.color, "gramos": f.gramos, "coste": f.gramos * f.eur_kg / 1000}
                for f in core.agregar(placas)]
        return {
            **({"html": documento.html(contexto_unico(datos, p, c, placas, d, siguiente_numero()))} if vista else {}),
            **totales(d, fils), **complejidad(d, datos.complejidad),
        }

    @app.post("/api/vista-previa", response_class=HTMLResponse)
    def vista_previa(datos: Datos):
        p, c, placas, d = construir_unico(datos)
        return documento.html(contexto_unico(datos, p, c, placas, d, siguiente_numero()))

    @app.post("/api/exportar")
    def exportar(datos: Datos):
        exigir_pdf()
        return respuesta_pdf(*_aplanar(final_unico(datos, generar_pdf)))

    @app.post("/api/registrar")
    def registrar(datos: Datos):
        numero, nombre, html = final_unico(datos, lambda ctx, nombre, c: documento.html(ctx))
        return {"numero": numero, "nombre": nombre, "html": html}

    @app.post("/api/combo/calcular")
    def calcular_combo(combo: Combo, vista: bool = False):
        c, piezas, actual, d_actual, d = construir_combo(combo)
        ctx = documento.contexto_combo(c, piezas, d, combo.cliente, siguiente_numero(), combo.desglose,
                                       margen=combo.margen) if vista else None
        fils = [{"perfil": f["perfil"], "color": f["color"], "gramos": f["gramos"], "coste": f["coste"]}
                for f in core.agregar_combo(piezas)]
        return {
            **({"html": documento.html(ctx)} if vista else {}),
            **totales(d, fils), "piezas": len(piezas), "actual": actual,
            "complejidad": complejidad(d_actual, combo.actual.complejidad) if d_actual else None,
        }

    @app.post("/api/combo/exportar")
    def exportar_combo(combo: Combo):
        exigir_pdf()
        return respuesta_pdf(*_aplanar(final_combo(combo, generar_pdf)))

    @app.post("/api/combo/registrar")
    def registrar_combo(combo: Combo):
        numero, nombre, html = final_combo(combo, lambda ctx, nombre, c: documento.html(ctx))
        return {"numero": numero, "nombre": nombre, "html": html}

    @app.get("/api/historial")
    def listar():
        return core.historial(db)

    @app.patch("/api/historial/{id_}")
    def pagado(id_: int, cuerpo: Pagado):
        if not core.marcar_pagado(db, id_, cuerpo.pagado):
            raise HTTPException(404, "Presupuesto no encontrado.")
        return {"id": id_, "pagado": cuerpo.pagado}

    return app


def _aplanar(resultado):
    numero, nombre, (pdf, ruta) = resultado
    return numero, nombre, pdf, ruta

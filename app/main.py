import asyncio
import base64
import logging
import os
import re
import secrets
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


class Datos(BaseModel):
    proyecto: str
    cliente: str = ""
    minutos: float = Field(ge=0)
    margen: float = Field(ge=0)
    eur_kg: dict[int, float] = {}
    tiempo_min: float | None = Field(default=None, ge=0)
    gramos: dict[int, float] = {}
    desglose: bool = True
    complejidad: bool = True
    notas: str = Field(default="", max_length=280)


class Pagado(BaseModel):
    pagado: bool


def destino_por_defecto():
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if url:
        return url
    if os.environ.get("VERCEL"):
        log.warning("Sin DATABASE_URL: el historial se guarda en /tmp y se perderá.")
        return Path("/tmp/registro.db")
    return BASE / "registro.db"


def crear_app(config=BASE / "config.toml", db=None):
    db = db or destino_por_defecto()
    clave = os.environ.get("APP_CLAVE")

    def cfg():
        return core.cargar_config(config)

    def construir(datos):
        c = cfg()
        segundos = datos.tiempo_min * 60 if datos.tiempo_min is not None else None
        try:
            p = core.deserializar(datos.proyecto)
            placas, d = core.presupuestar(p, c, datos.minutos, datos.margen, datos.eur_kg,
                                          segundos, datos.gramos, datos.complejidad)
        except core.ErrorPresupuesto as e:
            raise HTTPException(422, str(e))
        return p, c, placas, d

    def documento_final(datos, generar):
        p, c, placas, d = construir(datos)
        fichero = p.meta.get("_fichero", "modelo.3mf")
        base_nombre = re.sub(r"\.gcode$", "", Path(fichero).stem, flags=re.I)
        gramos = sum(f.gramos for pl in placas for f in pl.filamentos)

        def con_numero(numero):
            nombre = re.sub(r'[\\/:*?"<>|]+', "_", f"{c['emisor']['titulo_documento']}_{numero}_{base_nombre}")
            ctx = documento.contexto(c, p, placas, d, datos.cliente, numero, datos.desglose, base_nombre, nombre,
                                     datos.notas)
            return nombre, generar(ctx, nombre, c)

        _, numero, (nombre, resultado) = core.registrar(db, datos.cliente or "—", p.meta.get("Title") or base_nombre,
                                                        fichero, d, gramos, con_numero)
        log.info("Presupuesto %s: %s", numero, core.eur(d.precio))
        return numero, nombre, resultado

    app = FastAPI(title="Presupuesto 3D")
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.mount("/fonts", StaticFiles(directory=BASE / "fonts"), name="fonts")

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
        return FileResponse(BASE / "static" / "index.html")

    @app.get("/api/config")
    def configuracion():
        c = cfg()
        return {"pendientes": list(core.pendientes(c)), "marca": c["marca"]["nombre"],
                "acento": c["marca"]["color_acento"], "margen": c["produccion"]["margen"],
                "minutos": c["produccion"]["minutos_por_defecto"], "pdf_servidor": documento.pdf_disponible(),
                "complejidad": {k: c.get("complejidad", {}).get(k, 0) for k in ("minimo", "maximo")}}

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
            raise HTTPException(422, str(e))
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
                            "eur_kg": f.eur_kg} for f in p.filamentos.values()],
            "imagen": base64.b64encode(p.imagen).decode() if p.imagen else None,
            "avisos": p.avisos,
        }

    @app.post("/api/calcular")
    def calcular(datos: Datos):
        _, _, placas, d = construir(datos)
        fils = core.agregar(placas)
        return {
            "horas": d.horas, "material": d.material, "maquina": d.maquina, "mano_obra": d.mano_obra,
            "coste": d.coste, "precio": d.precio, "margen_redondeo": d.precio - d.coste,
            "ajuste": d.ajuste, "ajuste_pct": d.ajuste_pct, "nivel": d.nivel, "aplicado": datos.complejidad,
            "motivos": [{"titulo": t, "detalle": x, "pct": v} for t, x, v in d.motivos],
            "reparto": [{"clave": k, "nombre": n, "importe": v} for k, n, v in core.reparto(d)],
            "gramos": sum(f.gramos for f in fils),
            "filamentos": [{"id": f.id, "gramos": f.gramos, "coste": f.gramos * f.eur_kg / 1000} for f in fils],
        }

    @app.post("/api/vista-previa", response_class=HTMLResponse)
    def vista_previa(datos: Datos):
        p, c, placas, d = construir(datos)
        fichero = Path(p.meta.get("_fichero", "modelo.3mf")).stem
        return documento.html(documento.contexto(c, p, placas, d, datos.cliente, core.siguiente_numero(db),
                                                 datos.desglose, fichero, notas=datos.notas))

    @app.post("/api/exportar")
    def exportar(datos: Datos):
        if not documento.pdf_disponible():
            raise HTTPException(501, "Este servidor no puede generar PDF: usa /api/registrar e imprime desde el navegador.")

        def generar(ctx, nombre, c):
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

        numero, nombre, (pdf, ruta) = documento_final(datos, generar)
        cabeceras = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(nombre)}.pdf", "X-Numero": numero}
        if ruta:
            cabeceras["X-Ruta"] = quote(str(ruta))
        return Response(pdf, media_type="application/pdf", headers=cabeceras)

    @app.post("/api/registrar")
    def registrar(datos: Datos):
        numero, nombre, html = documento_final(datos, lambda ctx, nombre, c: documento.html(ctx))
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

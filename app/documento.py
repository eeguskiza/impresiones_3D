import base64
import functools
import mimetypes
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .core import agregar, agregar_combo, duracion_txt, eur, gramos_txt, nombre_color, perfil_corto

BASE = Path(__file__).resolve().parent.parent
entorno = Environment(loader=FileSystemLoader(BASE / "templates"), autoescape=select_autoescape())


def porcentaje(x):
    return f"{x * 100:+.0f} %"


def data_uri(datos, tipo="image/png"):
    return f"data:{tipo};base64,{base64.b64encode(datos).decode()}"


def logo_uri(ruta):
    if not ruta:
        return None
    p = Path(ruta) if Path(ruta).is_absolute() else BASE / ruta
    if not p.is_file():
        return None
    return data_uri(p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/png")


def base(cfg, d, cliente, numero, mostrar_desglose, titulo, detalle, imagenes, fils, titulo_pagina="",
         etiqueta_ajuste="Complejidad", nota_ajuste="", piezas=()):
    fils = list(fils)
    return {
        "marca": cfg["marca"], "emisor": cfg["emisor"], "logo": logo_uri(cfg["marca"].get("logo")),
        "numero": numero, "titulo_pagina": titulo_pagina, "fecha": date.today().strftime("%d/%m/%Y"), "cliente": cliente or "—",
        "titulo": titulo, "detalle_pieza": detalle, "imagenes": imagenes[:3],
        "alto_hero": max(40, 88 - max(0, len(fils) - 3) * 8 - (len(piezas) * 7 + 12 if piezas else 0)),
        "tiempo": duracion_txt(d.horas), "peso": gramos_txt(sum(f["gramos"] for f in fils)),
        "filamentos": [{"color": f["color"][:7], "nombre_color": nombre_color(f["color"]), "perfil": perfil_corto(f["perfil"]),
                        "gramos": gramos_txt(f["gramos"]), "coste": eur(f["coste"])} for f in fils],
        "piezas": [{"titulo": p["titulo"], "tiempo": duracion_txt(p["horas"]), "gramos": gramos_txt(p["gramos"])} for p in piezas],
        "mostrar_desglose": mostrar_desglose,
        "desglose": [("Material", eur(d.material)), ("Máquina, energía y fallos", eur(d.maquina))]
                    + ([(etiqueta_ajuste, eur(d.ajuste))] if round(d.ajuste, 2) else [])
                    + [("Preparación y acabado", eur(d.mano_obra)), ("Margen y redondeo", eur(d.precio - d.coste))],
        "nota_ajuste": nota_ajuste if round(d.ajuste, 2) else "",
        "total": eur(d.precio),
    }


def contexto(cfg, proyecto, placas, d, cliente, numero, mostrar_desglose, fichero="", titulo_pagina=""):
    meta = proyecto.meta
    detalle = " · ".join(filter(None, [f"Diseño de {meta['Designer']}" if meta.get("Designer") else "",
                                       proyecto.impresora, f"{len(placas)} placas" if len(placas) > 1 else ""]))
    imagenes = [data_uri(pl.imagen) for pl in placas if pl.imagen] or ([data_uri(proyecto.imagen)] if proyecto.imagen else [])
    fils = [{"color": f.color, "perfil": f.perfil, "gramos": f.gramos, "coste": f.gramos * f.eur_kg / 1000}
            for f in agregar(placas)]
    return base(cfg, d, cliente, numero, mostrar_desglose, meta.get("Title") or fichero, detalle, imagenes, fils,
                titulo_pagina, f"Complejidad ({porcentaje(d.ajuste_pct)})", " · ".join(m[0] for m in d.motivos))


def contexto_combo(cfg, piezas, d, cliente, numero, mostrar_desglose, titulo_pagina=""):
    imagenes = [f"data:image/png;base64,{p['imagen']}" for p in piezas if p.get("imagen")]
    return base(cfg, d, cliente, numero, mostrar_desglose, f"Pedido de {len(piezas)} piezas",
                " · ".join(p["titulo"] for p in piezas), imagenes, agregar_combo(piezas), titulo_pagina, piezas=piezas)


def html(ctx, raiz="/"):
    return entorno.get_template("presupuesto.html").render(**ctx, raiz=raiz)


@functools.cache
def pdf_disponible():
    try:
        import weasyprint  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


def pdf(ctx):
    from weasyprint import HTML
    return HTML(string=html(ctx, (BASE / "public").as_uri() + "/"), base_url=str(BASE)).write_pdf()

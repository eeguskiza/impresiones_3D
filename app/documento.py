import base64
import functools
import mimetypes
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .core import agregar, duracion_txt, eur, gramos_txt, nombre_color, perfil_corto

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


def contexto(cfg, proyecto, placas, d, cliente, numero, mostrar_desglose, fichero="", titulo_pagina="", notas=""):
    fils = agregar(placas)
    meta = proyecto.meta
    detalle = " · ".join(filter(None, [f"Diseño de {meta['Designer']}" if meta.get("Designer") else "",
                                       proyecto.impresora, f"{len(placas)} placas" if len(placas) > 1 else ""]))
    imagenes = [data_uri(pl.imagen) for pl in placas if pl.imagen][:3] or (
        [data_uri(proyecto.imagen)] if proyecto.imagen else [])
    return {
        "marca": cfg["marca"], "emisor": cfg["emisor"], "logo": logo_uri(cfg["marca"].get("logo")),
        "numero": numero, "titulo_pagina": titulo_pagina, "fecha": date.today().strftime("%d/%m/%Y"), "cliente": cliente or "—",
        "titulo": meta.get("Title") or fichero, "detalle_pieza": detalle, "imagenes": imagenes,
        "alto_hero": max(52, 88 - max(0, len(fils) - 3) * 8 - (32 if notas.strip() else 0)),
        "notas": notas.strip(),
        "tiempo": duracion_txt(d.horas), "peso": gramos_txt(sum(f.gramos for f in fils)),
        "filamentos": [{"color": f.color[:7], "nombre_color": nombre_color(f.color), "perfil": perfil_corto(f.perfil),
                        "gramos": gramos_txt(f.gramos), "coste": eur(f.gramos * f.eur_kg / 1000)} for f in fils],
        "mostrar_desglose": mostrar_desglose,
        "desglose": [("Material", eur(d.material)), ("Máquina, energía y fallos", eur(d.maquina))]
                    + ([(f"Complejidad ({porcentaje(d.ajuste_pct)})", eur(d.ajuste))] if d.ajuste else [])
                    + [("Preparación y acabado", eur(d.mano_obra)), ("Margen y redondeo", eur(d.precio - d.coste))],
        "nota_ajuste": " · ".join(m[0] for m in d.motivos) if d.ajuste else "",
        "total": eur(d.precio),
    }


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
    return HTML(string=html(ctx, BASE.as_uri() + "/"), base_url=str(BASE)).write_pdf()

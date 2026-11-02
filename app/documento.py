import base64
import functools
import mimetypes
import os
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


def base(cfg, d, cliente, numero, mostrar_desglose, titulo, detalle, imagenes, piezas, titulo_pagina="",
         margen=None, combo=False):
    prod = cfg["produccion"]
    margen = prod["margen"] if margen is None else margen
    fils = agregar_combo(piezas)
    resumen = lambda p: f"{duracion_txt(p['horas'])} · {gramos_txt(p['gramos'])}"
    linea = lambda concepto, detalle, valor=None, color="": {"concepto": concepto, "detalle": detalle, "valor": valor,
                                                             "color": color}

    def agrupar(nivel):
        grupos = []
        for p in piezas:
            if not mostrar_desglose:
                lineas = [] if combo else [linea(p["titulo"], resumen(p))]
            elif nivel == 3:
                extra = f" · complejidad {porcentaje(p['ajuste_pct'])}" if round(p["ajuste"], 2) else ""
                lineas = [linea(p["titulo"], resumen(p) + extra, p["material"] + p["maquina"] + p["ajuste"])]
            else:
                if nivel == 1:
                    lineas = [linea(perfil_corto(f["perfil"]), f"{nombre_color(f['color'])} · {gramos_txt(f['gramos'])} × "
                                    f"{eur(f['eur_kg'])}/kg", f["gramos"] * f["eur_kg"] / 1000, f["color"][:7])
                              for f in p["filamentos"]]
                else:
                    lineas = [linea("Material", " · ".join(f"{perfil_corto(f['perfil'])} {nombre_color(f['color']).lower()} "
                                                            f"{gramos_txt(f['gramos'])}" for f in p["filamentos"]),
                                    p["material"])]
                lineas.append(linea("Impresión", f"{duracion_txt(p['horas'])} de máquina: luz, desgaste y fallos",
                                    p["maquina"]))
                if round(p["ajuste"], 2):
                    lineas.append(linea(f"Complejidad {porcentaje(p['ajuste_pct'])}", " · ".join(p["motivos"]), p["ajuste"]))
            grupos.append({"titulo": p["titulo"] if combo and nivel < 3 else "", "detalle": resumen(p), "lineas": lineas})
        alto = lambda l: 9.5 if nivel == 2 and l["concepto"] == "Material" else 7
        return grupos, sum(7 * bool(g["titulo"]) + sum(alto(l) for l in g["lineas"]) for g in grupos)

    comunes = 2 if mostrar_desglose and round(d.mano_obra, 2) else 1
    for nivel in (1, 2, 3):
        grupos, filas = agrupar(nivel)
        tabla = 9 + 14 + 7 * comunes + filas
        if tabla <= 150:
            break
    compacta = tabla > 150
    if compacta:
        tabla = 9 + 14 + (tabla - 23) * 5.5 / 7
    comunes = []
    if mostrar_desglose:
        if round(d.mano_obra, 2):
            minutos = round(d.mano_obra / prod["mano_obra_eur_h"] * 60)
            comunes.append(linea("Preparación y acabado", f"{minutos} min × {eur(prod['mano_obra_eur_h'])}/h", d.mano_obra))
        mostrado = sum(round(l["valor"], 2) for g in grupos for l in g["lineas"]) + sum(round(l["valor"], 2) for l in comunes)
        redondeo = f" y redondeo a {eur(prod['redondeo_eur'])}" if prod["redondeo_eur"] else ""
        comunes.append(linea("Margen", f"{margen * 100:.0f} %{redondeo}", round(d.precio, 2) - mostrado))
    else:
        comunes.append(linea("Incluye", "Material, uso de máquina, preparación y margen"))
    for l in [l for g in grupos for l in g["lineas"]] + comunes:
        l["importe"] = "" if l["valor"] is None else eur(round(l["valor"], 2) + 0.0)
    alto = min(88, 156 - tabla)
    return {
        "marca": cfg["marca"], "emisor": cfg["emisor"], "logo": logo_uri(cfg["marca"].get("logo")),
        "numero": numero, "titulo_pagina": titulo_pagina, "fecha": date.today().strftime("%d/%m/%Y"), "cliente": cliente or "—",
        "titulo": titulo, "detalle_pieza": detalle, "imagenes": imagenes[:3], "alto_hero": alto if alto >= 40 else 0,
        "tiempo": duracion_txt(d.horas), "peso": gramos_txt(sum(f["gramos"] for f in fils)),
        "colores": [f["color"][:7] for f in fils][:8],
        "grupos": grupos, "comunes": comunes, "total": eur(d.precio), "compacta": compacta,
    }


def contexto(cfg, proyecto, placas, d, cliente, numero, mostrar_desglose, fichero="", titulo_pagina="", margen=None):
    meta = proyecto.meta
    detalle = " · ".join(filter(None, [f"Diseño de {meta['Designer']}" if meta.get("Designer") else "",
                                       proyecto.impresora, f"{len(placas)} placas" if len(placas) > 1 else ""]))
    imagenes = [data_uri(pl.imagen) for pl in placas if pl.imagen] or ([data_uri(proyecto.imagen)] if proyecto.imagen else [])
    fils = agregar(placas)
    pieza = {"titulo": meta.get("Title") or fichero, "horas": d.horas, "gramos": sum(f.gramos for f in fils),
             "material": d.material, "maquina": d.maquina, "ajuste": d.ajuste, "ajuste_pct": d.ajuste_pct,
             "motivos": [m[0] for m in d.motivos],
             "filamentos": [{"perfil": f.perfil, "color": f.color, "gramos": f.gramos, "eur_kg": f.eur_kg} for f in fils]}
    return base(cfg, d, cliente, numero, mostrar_desglose, pieza["titulo"], detalle, imagenes, [pieza], titulo_pagina, margen)


def contexto_combo(cfg, piezas, d, cliente, numero, mostrar_desglose, titulo_pagina="", margen=None):
    imagenes = [f"data:image/png;base64,{p['imagen']}" for p in piezas if p.get("imagen")]
    return base(cfg, d, cliente, numero, mostrar_desglose, f"Pedido de {len(piezas)} piezas",
                " · ".join(p["titulo"] for p in piezas), imagenes, piezas, titulo_pagina, margen, combo=True)


def html(ctx, raiz="/"):
    return entorno.get_template("presupuesto.html").render(**ctx, raiz=raiz)


@functools.cache
def pdf_disponible():
    if os.environ.get("VERCEL"):
        return False
    try:
        import weasyprint  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


def pdf(ctx):
    from weasyprint import HTML
    return HTML(string=html(ctx, (BASE / "public").as_uri() + "/"), base_url=str(BASE)).write_pdf()

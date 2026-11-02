import pytest

from app import core

MANUAL = {"segundos": 5 * 3600 + 20 * 60, "gramos": {1: 12, 2: 85.5, 3: 31}}


def test_parseo_ejemplo(ejemplo, cfg):
    p = core.leer_3mf(ejemplo, cfg)
    assert p.meta["Title"] == "Urban Spiderman"
    assert p.meta["Designer"] == "lucamoretti86"
    assert p.meta["License"] == "Standard Digital File License"
    assert p.impresora == "Bambu Lab H2C"
    assert not p.laminado
    assert p.imagen.startswith(b"\x89PNG")
    assert [(f.id, core.perfil_corto(f.perfil), f.color, f.eur_kg) for f in p.filamentos.values()] == [
        (1, "Bambu PLA Matte", "#FFFFFF", 24.99),
        (2, "Bambu PLA Basic", "#000000", 24.99),
        (3, "Bambu PLA Matte", "#DE4343", 24.99),
    ]
    assert [core.nombre_color(f.color) for f in p.filamentos.values()] == ["Blanco", "Negro", "Rojo"]
    assert [a[:2] for a in p.avisos] == [["aviso", "Para Bambu Lab H2C"], ["info", "Standard Digital File License"],
                                         ["aviso", "Sin laminar"]]


def test_calculo_valores_conocidos(ejemplo, cfg):
    p = core.leer_3mf(ejemplo, cfg)
    placas, d = core.presupuestar(p, cfg, 15, **MANUAL)
    assert d.horas == pytest.approx(16 / 3)
    assert d.material == pytest.approx(128.5 * 24.99 / 1000)
    assert d.mano_obra == pytest.approx(3.75)
    assert round(d.coste, 2) == 12.95
    assert d.precio == 14.50


def test_laminado_no_pide_datos(laminado, cfg):
    p = core.leer_3mf(laminado, cfg)
    assert p.laminado
    assert not any(a[1] == "Sin laminar" for a in p.avisos)
    _, d = core.presupuestar(p, cfg, 15)
    assert d.precio == 14.50


def test_errores(ejemplo, cfg):
    with pytest.raises(core.ErrorPresupuesto, match="no es un 3MF"):
        core.leer_3mf(b"basura", cfg)
    p = core.leer_3mf(ejemplo, cfg)
    with pytest.raises(core.ErrorPresupuesto, match="tiempo"):
        core.presupuestar(p, cfg, 15, gramos=MANUAL["gramos"])
    p.filamentos[2].eur_kg = None
    with pytest.raises(core.ErrorPresupuesto, match="Sin precio"):
        core.presupuestar(p, cfg, 15, **MANUAL)
    _, d = core.presupuestar(p, cfg, 15, eur_kg={2: 24.99}, **MANUAL)
    assert d.precio == 14.50


def test_complejidad(ejemplo, cfg):
    cfg = {**cfg, "complejidad": {"un_color": -0.05, "por_color_extra": 0.03, "pintado": 0.04, "soportes": 0.03,
                                  "horas_larga": 12, "larga": 0.03, "minimo": -0.05, "maximo": 0.15}}
    p = core.leer_3mf(ejemplo, cfg)
    assert p.soportes and p.pintado
    _, d = core.presupuestar(p, cfg, 15, **MANUAL)
    assert d.ajuste_pct == 0.10 and d.nivel == "Compleja"
    assert [m[0] for m in d.motivos] == ["3 colores", "Pintada a color", "Soportes"]
    assert d.ajuste == pytest.approx((d.coste - d.mano_obra - d.ajuste) * 0.10)
    _, sin = core.presupuestar(p, cfg, 15, complejidad=False, **MANUAL)
    assert sin.ajuste == 0 and sin.ajuste_pct == 0.10 and round(sin.coste, 2) == 12.95

    p.soportes = False
    _, d = core.presupuestar(p, cfg, 15, segundos=MANUAL["segundos"], gramos={2: 128.5})
    assert d.ajuste_pct == -0.05 and d.nivel == "Sencilla" and d.ajuste < 0


def test_serializar(laminado, cfg):
    p = core.leer_3mf(laminado, cfg)
    q = core.deserializar(core.serializar(p))
    assert q == p
    with pytest.raises(core.ErrorPresupuesto):
        core.deserializar("basura")


def test_config_local_y_entorno(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[emisor]\nnombre = "[REEMPLAZAR]"\nbizum = "[REEMPLAZAR]"\n[marca]\nnombre = "M"\n')
    (tmp_path / "config.local.toml").write_text('[emisor]\nnombre = "Local"\n')
    monkeypatch.setenv("EMISOR_BIZUM", "600111222")
    c = core.cargar_config(tmp_path / "config.toml")
    assert c["emisor"] == {"nombre": "Local", "bizum": "600111222"} and c["marca"]["nombre"] == "M"


def test_reparto_suma_el_precio(ejemplo, cfg):
    cfg = {**cfg, "complejidad": {"un_color": -0.05}}
    p = core.leer_3mf(ejemplo, cfg)
    for gramos in (MANUAL["gramos"], {2: 128.5}):
        _, d = core.presupuestar(p, cfg, 15, segundos=MANUAL["segundos"], gramos=gramos)
        partes = core.reparto(d)
        assert sum(v for _, _, v in partes) == pytest.approx(d.precio)
        assert all(v > 0 for _, _, v in partes)


def test_precio_por_perfil_sin_impresora(ejemplo, cfg):
    cfg = {**cfg, "filamentos_eur_kg": {"Bambu PLA Matte": 30.0, "PLA": 20.0}}
    fils = core.leer_3mf(ejemplo, cfg).filamentos
    assert [f.eur_kg for f in fils.values()] == [30.0, 20.0, 30.0]

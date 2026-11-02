import zipfile
from io import BytesIO
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
EJEMPLO = RAIZ / "ejemplos" / "Spiderman_urban.3mf"
SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="19200"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_m="4" used_g="12.00"/>
    <filament id="2" type="PLA" color="#000000" used_m="28" used_g="85.50"/>
    <filament id="3" type="PLA" color="#DE4343" used_m="10" used_g="31.00"/>
  </plate>
</config>"""
CONFIG = """
[app]
carpeta_salida = "presupuestos"

[marca]
nombre = "Marca"
logo = ""
color_principal = "#1B2433"
color_acento = "#FF5A36"

[emisor]
nombre = "Emisor"
bizum = "600000000"
titulo_documento = "Presupuesto"

[maquina]
precio_eur = 849.0
horas_amortizacion = 1000
potencia_media_w = 200
mantenimiento_eur_h = 0.10

[energia]
precio_kwh = 0.18

[produccion]
tasa_fallo = 0.08
mano_obra_eur_h = 15.0
minutos_por_defecto = 15
margen = 0.10
redondeo_eur = 0.5

[filamentos_eur_kg]
"""


@pytest.fixture
def cfg():
    import tomllib
    return tomllib.loads(CONFIG)


@pytest.fixture
def ejemplo():
    if not EJEMPLO.is_file():
        pytest.skip("Falta ejemplos/Spiderman_urban.3mf")
    return EJEMPLO.read_bytes()


@pytest.fixture
def laminado(ejemplo):
    salida = BytesIO()
    with zipfile.ZipFile(BytesIO(ejemplo)) as origen, zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        for i in origen.infolist():
            datos = SLICE_INFO.encode() if i.filename == "Metadata/slice_info.config" else origen.read(i)
            z.writestr(i.filename, datos)
    return salida.getvalue()


@pytest.fixture
def cliente(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import crear_app
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.replace('"presupuestos"', f'"{tmp_path / "salida"}"'), encoding="utf-8")
    with TestClient(crear_app(config, tmp_path / "registro.db")) as c:
        c.salida = tmp_path / "salida"
        yield c

DATOS = {"cliente": "Ejemplo", "minutos": 15, "margen": 0.10, "tiempo_min": 320, "gramos": {"1": 12, "2": 85.5, "3": 31}}


def analizar(cliente, contenido, nombre="Spiderman_urban.3mf"):
    r = cliente.post(f"/api/analizar?fichero={nombre}", content=contenido)
    assert r.status_code == 200, r.text
    return r.json()


def test_analizar_y_calcular(cliente, ejemplo):
    a = analizar(cliente, ejemplo)
    assert a["titulo"] == "Urban Spiderman" and not a["laminado"]
    assert a["filamentos"][0] == {"id": 1, "tipo": "PLA", "perfil": "Bambu PLA Matte", "color": "#FFFFFF",
                                  "nombre_color": "Blanco", "gramos": 0.0, "eur_kg": 24.99}
    assert a["imagen"]
    r = cliente.post("/api/calcular", json={**DATOS, "proyecto": a["proyecto"]})
    assert r.status_code == 200
    assert round(r.json()["coste"], 2) == 12.95 and r.json()["precio"] == 14.50
    r = cliente.post("/api/calcular", json={**DATOS, "proyecto": a["proyecto"], "tiempo_min": None})
    assert r.status_code == 422 and "tiempo" in r.json()["detail"]
    r = cliente.post("/api/calcular", json={**DATOS, "proyecto": "manipulado"})
    assert r.status_code == 422


def test_fichero_no_valido(cliente):
    r = cliente.post("/api/analizar?fichero=x.3mf", content=b"no es un zip")
    assert r.status_code == 422 and "3MF" in r.json()["detail"]


def test_exportar_numera_y_vista_previa_no(cliente, ejemplo):
    datos = {**DATOS, "proyecto": analizar(cliente, ejemplo)["proyecto"]}
    for _ in range(2):
        r = cliente.post("/api/vista-previa", json=datos)
        assert r.status_code == 200 and "14,50 €" in r.text
    assert cliente.get("/api/historial").json() == []

    numeros = []
    for _ in range(2):
        r = cliente.post("/api/exportar", json=datos)
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        numeros.append(r.headers["X-Numero"])
    anio = numeros[0][:4]
    assert numeros == [f"{anio}-0001", f"{anio}-0002"]
    assert sorted(p.name for p in cliente.salida.iterdir()) == [
        f"Presupuesto_{anio}-0001_Spiderman_urban.pdf", f"Presupuesto_{anio}-0002_Spiderman_urban.pdf"]

    r = cliente.post("/api/registrar", json=datos)
    assert r.json()["numero"] == f"{anio}-0003" and "<title>Presupuesto_" in r.json()["html"]

    historial = cliente.get("/api/historial").json()
    assert [h["numero"] for h in historial] == [f"{anio}-0003", *reversed(numeros)]
    r = cliente.patch(f"/api/historial/{historial[0]['id']}", json={"pagado": True})
    assert r.status_code == 200
    assert cliente.get("/api/historial").json()[0]["pagado"] is True


def test_laminado_sin_datos_manuales(cliente, laminado):
    a = analizar(cliente, laminado, "Spiderman_urban.gcode.3mf")
    assert a["laminado"] and a["filamentos"][1]["gramos"] == 85.5
    r = cliente.post("/api/calcular", json={"proyecto": a["proyecto"], "minutos": 15, "margen": 0.10})
    assert r.status_code == 200 and r.json()["precio"] == 14.50


def test_clave_de_acceso(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import crear_app
    monkeypatch.setenv("APP_CLAVE", "secreta")
    c = TestClient(crear_app(db=tmp_path / "r.db"))
    assert c.get("/api/config").status_code == 401
    assert c.get("/api/config", auth=("yo", "mala")).status_code == 401
    assert c.get("/api/config", auth=("yo", "secreta")).status_code == 200


def test_calcular_con_vista(cliente, ejemplo):
    datos = {**DATOS, "proyecto": analizar(cliente, ejemplo)["proyecto"]}
    assert "html" not in cliente.post("/api/calcular", json=datos).json()
    r = cliente.post("/api/calcular?vista=true", json=datos).json()
    assert r["precio"] == 14.50 and "14,50 €" in r["html"]
    assert cliente.get("/api/historial").json() == []

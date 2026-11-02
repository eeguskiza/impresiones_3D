import logging
import platform
import socket
import subprocess
import threading
import time
import webbrowser

import uvicorn

from . import core
from .main import BASE, crear_app

log = logging.getLogger("presupuesto3d")


def abrir_navegador(url):
    if "microsoft" in platform.uname().release.lower():
        subprocess.run(["cmd.exe", "/c", "start", "", url], cwd="/mnt/c", stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def puerto_libre(inicial, intentos=10):
    for puerto in range(inicial, inicial + intentos):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", puerto)) != 0:
                return puerto
    raise SystemExit(f"No hay puertos libres entre {inicial} y {inicial + intentos - 1}.")


def main():
    cfg = core.cargar_config(BASE / "config.toml").get("app", {})
    nivel = cfg.get("nivel_log", "INFO").upper()
    logging.basicConfig(level=nivel, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    deseado = int(cfg.get("puerto", 8000))
    puerto = puerto_libre(deseado)
    if puerto != deseado:
        log.warning("El puerto %s está ocupado; uso el %s.", deseado, puerto)
    servidor = uvicorn.Server(uvicorn.Config(crear_app(), host="127.0.0.1", port=puerto, log_level=nivel.lower()))
    url = f"http://127.0.0.1:{puerto}/"

    def al_arrancar():
        while not servidor.started and not servidor.should_exit:
            time.sleep(0.1)
        if servidor.started:
            log.info("Abriendo %s", url)
            abrir_navegador(url)

    threading.Thread(target=al_arrancar, daemon=True).start()
    servidor.run()


if __name__ == "__main__":
    main()

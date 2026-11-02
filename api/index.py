import logging
import os

from app.main import crear_app

logging.basicConfig(level=os.environ.get("NIVEL_LOG", "INFO"))
app = crear_app()

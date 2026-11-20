#!/usr/bin/env python3
"""
Mini servidor para descargar el inventario de oportunidades desde el navegador.
Protegido con usuario y contraseña (DESCARGA_USUARIO / DESCARGA_PASSWORD).
Solo sirve los archivos de TIPOS; nunca los JSON de trabajo ni el .env.

Además lanza cada noche la actualización incremental (python fichas.py actualizar) a la hora de
ACTUALIZACION_HORA (por defecto 03:00, hora de ACTUALIZACION_ZONA, Europe/Madrid). Se desactiva con
ACTUALIZACION=no. El resultado queda en datos/fichas/log_nocturno.txt.
Después rehace la selección de fotos de las oportunidades (fotos.py archivo, huellas, informe y seleccionar), que el
etiquetado nocturno de box-etiquetar descarga más tarde. Se desactiva con FOTOS_NOCTURNO=no.
"""
import base64
import hmac
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path(os.getenv("DATA_DIR", "datos"))
USUARIO = os.getenv("DESCARGA_USUARIO", "alegria")
PASSWORD = os.getenv("DESCARGA_PASSWORD", "")
PUERTO = int(os.getenv("PORT", "8080"))
ACTUALIZAR = os.getenv("ACTUALIZACION", "si").lower() not in ("no", "0", "false") and bool(os.getenv("ANTHROPIC_API_KEY"))
HORA = os.getenv("ACTUALIZACION_HORA", "03:00")
ZONA = ZoneInfo(os.getenv("ACTUALIZACION_ZONA", "Europe/Madrid"))
LOG_NOCHE = DATA / "fichas" / "log_nocturno.txt"
ULTIMA = DATA / ".ultima_actualizacion"

TIPOS = {"resumen.txt": "text/plain; charset=utf-8",
         "desglose.txt": "text/plain; charset=utf-8",
         "inventario.csv": "text/csv; charset=utf-8",
         "huecos.csv": "text/csv; charset=utf-8",
         "negocios_sin_carpeta.csv": "text/csv; charset=utf-8",
         "inventario.json": "application/json; charset=utf-8",
         "revision.csv": "text/csv; charset=utf-8",
         "indice_oportunidades.json": "application/json; charset=utf-8",
         "log.txt": "text/plain; charset=utf-8",
         "log_todo.txt": "text/plain; charset=utf-8",
         "log_nocturno.txt": "text/plain; charset=utf-8",
         "resumen_fotos.txt": "text/plain; charset=utf-8",
         "fotos_oportunidades.csv": "text/csv; charset=utf-8",
         "fotos_por_oportunidad.csv": "text/csv; charset=utf-8",
         "log_fotos.txt": "text/plain; charset=utf-8",
         "seleccion_fotos.txt": "text/plain; charset=utf-8",
         "seleccion_fotos.csv": "text/csv; charset=utf-8"}


def disponibles():
    """resultados de datos/ y de sus subcarpetas (una por tanda: datos/2025/...)."""
    salida = {}
    for nombre, tipo in TIPOS.items():
        if (DATA / nombre).exists():
            salida[f"/{nombre}"] = (DATA / nombre, tipo)
        for sub in sorted(p for p in DATA.iterdir() if p.is_dir()):
            if (sub / nombre).exists() and sub.name.replace("-", "").replace("_", "").isalnum():
                salida[f"/{sub.name}/{nombre}"] = (sub / nombre, tipo)
    return salida


class Manejador(BaseHTTPRequestHandler):
    def _texto(self, codigo, texto, tipo="text/plain; charset=utf-8"):
        cuerpo = texto.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _autorizado(self):
        esperado = "Basic " + base64.b64encode(f"{USUARIO}:{PASSWORD}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), esperado)

    def do_GET(self):
        if not PASSWORD:
            return self._texto(503, "Descargas desactivadas: define DESCARGA_PASSWORD en el entorno.")
        if not self._autorizado():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="oportunidades"')
            self.end_headers()
            return
        archivos = disponibles()
        if self.path == "/":
            filas = [f'<li><a href="{ruta}">{ruta.lstrip("/")}</a> · {p.stat().st_size / 1024:.0f} KB</li>'
                     for ruta, (p, _) in archivos.items()] or ["<li>Todavía no hay resultados.</li>"]
            html = ("<!doctype html><meta charset='utf-8'><title>Inventario de oportunidades</title>"
                    "<body style='font-family:sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem'>"
                    "<h1>Inventario de oportunidades</h1><ul>" + "".join(filas) + "</ul></body>")
            return self._texto(200, html, "text/html; charset=utf-8")
        if self.path in archivos:
            p, tipo = archivos[self.path]
            nombre = self.path.strip("/").replace("/", "_")
            cuerpo = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)
            return
        self._texto(404, "No encontrado")


def programador():
    """Cada medio minuto mira la hora; a ACTUALIZACION_HORA lanza la actualización una vez al día."""
    while True:
        try:
            ahora = datetime.now(ZONA)
            hoy = ahora.strftime("%Y-%m-%d")
            hecha = ULTIMA.read_text().strip() if ULTIMA.exists() else ""
            if ahora.strftime("%H:%M") >= HORA and hecha != hoy:
                ULTIMA.write_text(hoy)
                LOG_NOCHE.parent.mkdir(parents=True, exist_ok=True)
                if LOG_NOCHE.exists() and LOG_NOCHE.stat().st_size > 5_000_000:   # se queda con lo último
                    LOG_NOCHE.write_bytes(LOG_NOCHE.read_bytes()[-2_000_000:])
                with LOG_NOCHE.open("a", encoding="utf-8") as log:
                    log.write(f"\n--- lanzada {ahora:%Y-%m-%d %H:%M} ({ZONA.key}) ---\n")
                    log.flush()
                    pasos = [["fichas.py", "actualizar"]]
                    if os.getenv("FOTOS_NOCTURNO", "si").lower() not in ("no", "0", "false"):
                        pasos += [["fotos.py", "archivo"], ["fotos.py", "huellas"], ["fotos.py", "informe"],
                                  ["fotos.py", "seleccionar"]]
                    for paso in pasos:
                        log.write(f"\n--- {' '.join(paso)} · {datetime.now(ZONA):%H:%M} ---\n")
                        log.flush()
                        subprocess.run([sys.executable, *paso], stdout=log, stderr=subprocess.STDOUT,
                                       cwd=Path(__file__).parent, check=False)
        except Exception as e:  # noqa: BLE001 — el programador no debe tumbar el servidor
            print(f"[actualización] error: {e}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"Servidor de descargas en el puerto {PUERTO}")
    if ACTUALIZAR:
        if not ULTIMA.exists():   # recién instalado: la primera actualización, esta noche
            ULTIMA.write_text(datetime.now(ZONA).strftime("%Y-%m-%d"))
        threading.Thread(target=programador, daemon=True).start()
        print(f"Actualización automática cada día a las {HORA} ({ZONA.key})")
    else:
        print("Actualización automática desactivada (ACTUALIZACION=no o falta ANTHROPIC_API_KEY)")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()

#!/usr/bin/env python3
"""
Inventario de fotos de las oportunidades · sin IA, sin coste de API de Claude
alegria-activity

¿Cuántas imágenes hay dentro de las carpetas de Oportunidades, cuántas están ya en el archivo de fotos
(el que etiquetó box-etiquetar y usa buscar_fotos) y cuánto costaría etiquetar con Haiku las que faltan?

Compara por la huella SHA1 que Box guarda de cada archivo: si el mismo archivo está copiado en el archivo de
fotos, es la misma foto aunque tenga otro nombre o esté en otra carpeta.

Uso (necesita haber pasado antes 'python inventario.py box' y, mejor, 'python inventario.py cruzar'):
  python fotos.py archivo             Lista las carpetas del archivo de fotos con su SHA1 (datos/fotos/archivo.json)
  python fotos.py huellas [--forzar]  Pide a Box el SHA1 de las imágenes de las oportunidades que aún no lo tienen;
                                      reanuda si se corta y no repite las carpetas que no han cambiado
  python fotos.py informe             Cruza ambas cosas y genera los informes en datos/fotos/
  python fotos.py todo [--forzar]     Las tres cosas seguidas (archivo solo si falta o con --forzar)

Solo LEE de Box. No escribe, mueve ni borra nada.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from inventario import (BOX_JSON, DATA, DIR_CARPETAS, HILOS, INV_JSON, ahora, box_get, escribir_json, leer_json,
                        normal)

# Carpetas raíz del archivo de fotos ya etiquetado (las tandas de box-etiquetar):
#   6895313990 archivo completo · 379464633435 Web 2025 / Fotos · 302472928787 archivo 2025
FOTOS_CARPETAS = [x.strip() for x in os.getenv("FOTOS_CARPETAS", "6895313990,379464633435,302472928787").split(",")
                  if x.strip()]
# Las mismas extensiones que etiqueta box-etiquetar
EXTENSIONES = {"jpg", "jpeg", "png", "webp", "heic", "heif", "tif", "tiff"}
MIN_KB = int(os.getenv("FOTOS_MIN_KB", "60"))                 # por debajo: iconos, firmas, logos pequeños
COSTE_FOTO = float(os.getenv("COSTE_FOTO", "0.00045"))         # USD por foto con Haiku en Batch (el del etiquetado)

DIR_FOTOS = DATA / "fotos"
ARCHIVO_JSON = DIR_FOTOS / "archivo.json"
HUELLAS_JSON = DIR_FOTOS / "huellas.json"
CSV_FOTOS = DIR_FOTOS / "fotos_oportunidades.csv"
CSV_POR_OP = DIR_FOTOS / "fotos_por_oportunidad.csv"
RESUMEN = DIR_FOTOS / "resumen_fotos.txt"

PALABRAS_NO_FOTO = ["logo", "logotipo", "firma", "captura", "screenshot", "pantallazo", "icono", "icon",
                    "plantilla", "boceto", "render", "mockup", "montaje", "infografia", "cartel", "plano"]


# ---------------------------------------------------------------- Box
def items(folder_id):
    """Elementos de una carpeta con SHA1 (de 1000 en 1000). None si ya no existe."""
    salida, offset = [], 0
    while True:
        r = box_get(f"/folders/{folder_id}/items",
                    {"fields": "id,type,name,size,extension,sha1", "limit": 1000, "offset": offset})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        salida += j["entries"]
        offset += len(j["entries"])
        if not j["entries"] or offset >= j.get("total_count", 0):
            return salida


def recorrer_por_niveles(frontera, entrar, recoger, etiqueta=""):
    """Recorre varios árboles a la vez, nivel a nivel y en paralelo.
    frontera: [(clave, folder_id, ruta)] · entrar(clave, ruta) → bool · recoger(clave, ruta, item)."""
    nivel = 0
    while frontera:
        nivel += 1
        siguiente = []
        with ThreadPoolExecutor(HILOS) as ex:
            for (clave, _fid, ruta), hijos in zip(frontera, ex.map(lambda t: items(t[1]), frontera)):
                for it in hijos or []:
                    sub = f"{ruta}/{it['name']}" if ruta else it["name"]
                    if it["type"] == "folder":
                        if it.get("size", 1) and entrar(clave, sub):
                            siguiente.append((clave, it["id"], sub))
                    elif it["type"] == "file":
                        recoger(clave, ruta, it)
        if etiqueta:
            print(f"  {etiqueta} · nivel {nivel}: {len(frontera)} carpetas leídas", flush=True)
        frontera = siguiente


def es_imagen(nombre, ext):
    return (ext or "").lower() in EXTENSIONES and not nombre.startswith("._")


# ---------------------------------------------------------------- 1. archivo de fotos
def cmd_archivo(_args):
    t0 = time.time()
    fotos = {}          # sha1 → {"ids": [...], "rutas": [...]}
    sin_sha1 = 0

    def recoger(raiz, ruta, it):
        nonlocal sin_sha1
        if not es_imagen(it["name"], it.get("extension")):
            return
        clave = it.get("sha1") or f"id:{it['id']}"
        if not it.get("sha1"):
            sin_sha1 += 1
        f = fotos.setdefault(clave, {"ids": [], "rutas": []})
        f["ids"].append(it["id"])
        f["rutas"].append(f"{raiz}:{ruta}")

    print(f"Archivo de fotos: carpetas {', '.join(FOTOS_CARPETAS)}")
    recorrer_por_niveles([(r, r, "") for r in FOTOS_CARPETAS], lambda *_: True, recoger, "archivo")
    archivos = sum(len(f["ids"]) for f in fotos.values())
    escribir_json(ARCHIVO_JSON, {"generado": ahora(), "carpetas": FOTOS_CARPETAS, "fotos": fotos}, compacto=True)
    print(f"Archivo de fotos: {archivos} imágenes, {len(fotos)} distintas"
          f"{f', {sin_sha1} sin SHA1' if sin_sha1 else ''} · {time.time() - t0:.0f} s")


# ---------------------------------------------------------------- 2. huellas de las oportunidades
def imagenes_de(op_id):
    p = DIR_CARPETAS / f"{op_id}.json"
    if not p.exists():
        return []
    return [a for a in json.loads(p.read_text("utf-8")) if es_imagen(a["nombre"], a.get("ext"))]


def cmd_huellas(args):
    bx = leer_json(BOX_JSON)
    if not bx:
        sys.exit("Falta datos/box_oportunidades.json: ejecuta antes 'python inventario.py box'.")
    huellas = {} if args.forzar else (leer_json(HUELLAS_JSON, {}) or {})
    pendientes = []
    reutilizadas = con_sha1 = 0
    for op in bx["oportunidades"]:
        imgs = imagenes_de(op["id"])
        if not imgs:
            huellas.pop(op["id"], None)
            continue
        previo = huellas.get(op["id"])
        if previo and previo.get("modificada") == op.get("modificada"):
            reutilizadas += 1
            continue
        if all(a.get("sha1") for a in imgs):              # el inventario ya lo trajo (versiones nuevas)
            huellas[op["id"]] = {"modificada": op.get("modificada"), "sha1": {a["id"]: a["sha1"] for a in imgs}}
            con_sha1 += 1
            continue
        necesarias = {a["ruta"] for a in imgs if a.get("sensibilidad") != "personal"}   # no entra en lo personal
        if not necesarias:
            huellas[op["id"]] = {"modificada": op.get("modificada"), "sha1": {}}
            continue
        pendientes.append((op, necesarias))
    print(f"Oportunidades con imágenes: {reutilizadas + con_sha1 + len(pendientes)} · "
          f"ya hechas: {reutilizadas + con_sha1} · a consultar en Box: {len(pendientes)}")

    t0 = time.time()
    bloque = 200
    for i in range(0, len(pendientes), bloque):
        trozo = pendientes[i:i + bloque]
        rutas = {op["id"]: necesarias for op, necesarias in trozo}
        encontrado = defaultdict(dict)

        def entrar(op_id, sub):          # solo baja por las carpetas que llevan a alguna imagen
            return any(r == sub or r.startswith(sub + "/") for r in rutas[op_id])

        def recoger(op_id, _ruta, it):
            if es_imagen(it["name"], it.get("extension")) and it.get("sha1"):
                encontrado[op_id][it["id"]] = it["sha1"]

        recorrer_por_niveles([(op["id"], op["id"], "") for op, _ in trozo], entrar, recoger)
        for op, _ in trozo:
            huellas[op["id"]] = {"modificada": op.get("modificada"), "sha1": encontrado.get(op["id"], {})}
        escribir_json(HUELLAS_JSON, huellas, compacto=True)       # guarda el avance: si se corta, reanuda aquí
        hechas = min(i + bloque, len(pendientes))
        print(f"  {hechas}/{len(pendientes)} oportunidades · {time.time() - t0:.0f} s", flush=True)
    escribir_json(HUELLAS_JSON, huellas, compacto=True)
    print(f"Huellas guardadas: {HUELLAS_JSON}")


# ---------------------------------------------------------------- 3. informe
def tipo_probable(a):
    """Sin IA: lo que probablemente es la imagen, por su carpeta y su nombre."""
    n = normal(a["nombre"])
    cat = normal(a.get("categoria", ""))
    if any(re.search(rf"(^| ){normal(p)}", n) for p in PALABRAS_NO_FOTO):
        if "logo" in n or "firma" in n or "icon" in n:
            return "logo o firma"
        if "plano" in n:
            return "plano"
        return "diseño o mockup"
    if cat.startswith("fotos"):
        return "foto de campaña"
    if "diseno" in cat:
        return "diseño o mockup"
    if "plano" in cat or "mobiliario" in cat:
        return "plano"
    if a.get("parte") == "proyecto":
        return "foto (otra carpeta del proyecto)"
    return "imagen de propuesta"


def cmd_informe(_args):
    bx = leer_json(BOX_JSON)
    arch = leer_json(ARCHIVO_JSON)
    huellas = leer_json(HUELLAS_JSON, {}) or {}
    if not bx or not arch:
        sys.exit("Faltan datos: ejecuta antes 'python inventario.py box' y 'python fotos.py archivo'.")
    inv = {f["carpeta_id"]: f for f in (leer_json(INV_JSON, {}) or {}).get("oportunidades", [])}
    if not inv:
        print("  (sin inventario.json: el informe saldrá sin cliente, año ni situación; ejecuta 'inventario.py cruzar')")
    en_archivo = arch["fotos"]
    ids_archivo = {i for f in en_archivo.values() for i in f["ids"]}

    filas, por_op = [], []
    vistas = {}                     # sha1 → primera oportunidad en la que sale (para repetidas entre oportunidades)
    for op in bx["oportunidades"]:
        imgs = imagenes_de(op["id"])
        if not imgs:
            continue
        info = inv.get(op["id"], {})
        sha = (huellas.get(op["id"]) or {}).get("sha1", {})
        c = Counter()
        for a in imgs:
            s = a.get("sha1") or sha.get(a["id"]) or ""
            if a.get("sensibilidad") == "personal":
                estado = "excluida: personal"
            elif (a.get("size") or 0) < MIN_KB * 1024:
                estado = f"excluida: < {MIN_KB} KB"
            elif a["id"] in ids_archivo or (s and s in en_archivo):
                estado = "ya en el archivo de fotos"
            elif not s:
                estado = "sin huella"
            elif s in vistas:
                estado = "repetida (ya contada)"
            else:
                estado = "pendiente de etiquetar"
            if s and s not in vistas:
                vistas[s] = op["id"]
            tipo = tipo_probable(a)
            c[estado] += 1
            if estado == "pendiente de etiquetar":
                c[f"pend:{a.get('parte')}"] += 1
                c[f"tipo:{tipo}"] += 1
            filas.append({
                "estado": estado, "tipo_probable": tipo, "parte": a.get("parte", ""),
                "categoria": a.get("categoria", ""), "kb": round((a.get("size") or 0) / 1024),
                "nombre": a["nombre"], "ruta": a["ruta"], "file_id": a["id"], "sha1": s,
                "enlace_box": f"https://app.box.com/file/{a['id']}",
                "oportunidad_id": op["id"], "oportunidad": op["nombre"], "pais": op.get("pais", ""),
                "situacion": info.get("situacion", ""), "cliente": info.get("cliente", ""),
                "anio": info.get("anio", ""), "negocio_id": info.get("negocio_id", ""),
                "ids_en_archivo": " ".join(en_archivo[s]["ids"][:3]) if s in en_archivo else "",
            })
        por_op.append({
            "oportunidad_id": op["id"], "oportunidad": op["nombre"], "situacion": info.get("situacion", ""),
            "cliente": info.get("cliente", ""), "anio": info.get("anio", ""), "imagenes": len(imgs),
            "pendientes": c["pendiente de etiquetar"], "pendientes_proyecto": c["pend:proyecto"],
            "ya_en_archivo": c["ya en el archivo de fotos"], "repetidas": c["repetida (ya contada)"],
            "excluidas": sum(v for k, v in c.items() if k.startswith("excluida")), "sin_huella": c["sin huella"],
            "fotos_de_campana_pendientes": c["tipo:foto de campaña"],
            "enlace_box": f"https://app.box.com/folder/{op['id']}",
        })

    por_op.sort(key=lambda f: f["pendientes"], reverse=True)
    _csv(CSV_FOTOS, filas)
    _csv(CSV_POR_OP, por_op)
    _resumen(filas, por_op, arch)
    print(RESUMEN.read_text("utf-8"))


def _csv(p, filas):
    p.parent.mkdir(parents=True, exist_ok=True)
    if not filas:
        p.write_text("", "utf-8")
        return
    with p.open("w", newline="", encoding="utf-8-sig") as fh:   # ; y utf-8-sig: Excel lo abre bien
        w = csv.DictWriter(fh, fieldnames=list(filas[0]), delimiter=";")
        w.writeheader()
        w.writerows(filas)


def _resumen(filas, por_op, arch):
    pend = [f for f in filas if f["estado"] == "pendiente de etiquetar"]
    archivos_arch = sum(len(f["ids"]) for f in arch["fotos"].values())
    L = [f"FOTOS DE LAS OPORTUNIDADES · {ahora()} UTC", "",
         f"Archivo de fotos (ya etiquetado): {archivos_arch} imágenes, {len(arch['fotos'])} distintas "
         f"(listado {arch['generado']})",
         f"Imágenes en Oportunidades: {len(filas)} en {len(por_op)} oportunidades", ""]
    L.append("Estado:")
    for k, v in Counter(f["estado"] for f in filas).most_common():
        L.append(f"  {v:7d}  {k}")
    L += ["", f"PENDIENTES DE ETIQUETAR (distintas, no están en el archivo): {len(pend)}",
          f"  coste orientativo con Haiku: ≈ {len(pend) * COSTE_FOTO:.2f} $ "
          f"(solo las de Proyecto: ≈ {sum(1 for f in pend if f['parte'] == 'proyecto') * COSTE_FOTO:.2f} $)", ""]
    for titulo, clave in [("Por parte", "parte"), ("Por tipo probable (sin IA, por carpeta y nombre)", "tipo_probable"),
                          ("Por situación de la oportunidad", "situacion")]:
        L.append(f"{titulo}:")
        for k, v in Counter(f[clave] or "(sin dato)" for f in pend).most_common():
            L.append(f"  {v:7d}  {k}")
        L.append("")
    L.append("Por año:")
    for k, v in sorted(Counter(str(f["anio"] or "(sin año)") for f in pend).items()):
        L.append(f"  {v:7d}  {k}")
    L += ["", "Oportunidades con más fotos pendientes:"]
    for o in por_op[:25]:
        if not o["pendientes"]:
            break
        L.append(f"  {o['pendientes']:6d}  {o['oportunidad'][:60]}  [{o['situacion'] or 'sin cruzar'}]"
                 f"  (ya en archivo: {o['ya_en_archivo']})")
    L += ["", "Detalle: fotos_oportunidades.csv (una fila por imagen) y fotos_por_oportunidad.csv.",
          "No se ha enviado nada a la IA."]
    RESUMEN.write_text("\n".join(L) + "\n", "utf-8")


# ---------------------------------------------------------------- 4. selección con tope (sin IA)
# Carpetas que no se etiquetan: fotos de los visitantes (cantidad enorme, gente anónima, menores), memes, fondos
# y "Baja calidad" (copia en peor resolución de "Alta calidad": otro SHA1, la misma foto)
PALABRAS_VISITANTES = [x.strip() for x in os.getenv(
    "FOTOS_EXCLUIR_CARPETAS",
    "photocall,fotocall,fotomaton,photobooth,selfie,participantes,visitantes,asistentes,usuarios,publico,"
    "ganadores,sorteo,concursantes,alumnos,ninos,colegios,escolares,familias,retratos,polaroid,impresion,"
    "impresas,photo booth,fotos usuarios,fotos publico,memes,meme,campeones,sent,baja calidad,"
    "fondos pantalla,fondospantallas").split(",") if x.strip()]
TIPOS_FOTO = {"foto de campaña", "foto (otra carpeta del proyecto)"}
SELECCION_CSV = DIR_FOTOS / "seleccion_fotos.csv"
SELECCION_TXT = DIR_FOTOS / "seleccion_fotos.txt"


def _orden_repartido(n):
    """Índices 0..n-1 en orden 'repartido' (0, n/2, n/4, 3n/4…): coger los k primeros da k fotos espaciadas
    a lo largo de la carpeta, no las k primeras de una ráfaga."""
    vistos, salida, paso = set(), [], 1
    while len(salida) < n:
        for j in range(paso):
            i = int((2 * j + 1) * n / (2 * paso)) if paso > 1 else 0
            if i < n and i not in vistos:
                vistos.add(i)
                salida.append(i)
        paso *= 2
        if paso > 2 * n:
            salida += [i for i in range(n) if i not in vistos]
            break
    return salida


def _leer_csv(p):
    with p.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


_RX_EXCLUIR = None


def _motivo_exclusion(ruta, tiene_alta):
    """Palabra y carpeta por la que se excluye la ruta, o None. Palabra completa (admite plural), para que
    'sent' no salte en 'sentados'. 'Baja calidad' solo se excluye si la oportunidad tiene también 'Alta calidad'."""
    global _RX_EXCLUIR
    if _RX_EXCLUIR is None:
        alternativas = "|".join(sorted((re.escape(normal(p)) for p in PALABRAS_VISITANTES), key=len, reverse=True))
        _RX_EXCLUIR = re.compile(rf"(?:^| )({alternativas})(?:e?s)?(?= |$)")
    for seg in (ruta or "").split("/"):
        m = _RX_EXCLUIR.search(normal(seg))
        if m and not (m.group(1) == "baja calidad" and not tiene_alta):
            return m.group(1), seg
    return None


def _elegir(candidatas, cupo):
    """Hasta 'cupo' fotos, repartidas entre subcarpetas (por turnos) y a lo largo de cada subcarpeta."""
    if cupo <= 0:
        return []
    por_dir = defaultdict(list)
    for f in candidatas:
        por_dir[f["ruta"]].append(f)
    colas = []
    for ruta in sorted(por_dir, key=lambda r: -len(por_dir[r])):
        lista = sorted(por_dir[ruta], key=lambda f: f["nombre"])
        colas.append([lista[i] for i in _orden_repartido(len(lista))])
    elegidas = []
    while len(elegidas) < cupo and any(colas):
        for c in colas:
            if c and len(elegidas) < cupo:
                elegidas.append(c.pop(0))
    return elegidas


def _cupo(tope, minimo, ya_en_archivo):
    """Las oportunidades que ya tienen fotos en el archivo necesitan menos: tope menos lo que ya hay, con un mínimo."""
    return tope if not ya_en_archivo else max(minimo, tope - ya_en_archivo)


def cmd_seleccionar(args):
    if not CSV_FOTOS.exists():
        sys.exit("Falta fotos_oportunidades.csv: ejecuta antes 'python fotos.py informe'.")
    filas = _leer_csv(CSV_FOTOS)
    ya = Counter(f["oportunidad_id"] for f in filas if f["estado"] == "ya en el archivo de fotos")
    pend = [f for f in filas if f["estado"] == "pendiente de etiquetar"]

    con_alta = {f["oportunidad_id"] for f in filas if "alta calidad" in normal(f["ruta"])}
    descartes = Counter()
    carpetas_visit = Counter()
    candidatas = defaultdict(list)
    for f in pend:
        motivo = None
        if f["tipo_probable"] not in TIPOS_FOTO and not (args.con_propuesta and f["parte"] == "propuesta"):
            descartes[f"no es foto de campaña ({f['tipo_probable']})"] += 1
        elif (motivo := _motivo_exclusion(f["ruta"], f["oportunidad_id"] in con_alta)):
            descartes["carpeta excluida (visitantes, memes, baja calidad…)"] += 1
            carpetas_visit[(motivo[0], motivo[1][:40], f["oportunidad"][:35])] += 1
        else:
            candidatas[f["oportunidad_id"]].append(f)

    elegidas = []
    for op_id, lista in candidatas.items():
        elegidas += _elegir(lista, _cupo(args.tope, args.minimo, ya[op_id]))

    # Simulación con otros topes, para decidir
    sim = []
    for t in sorted({10, 20, 30, 50, 80, 100, args.tope}):
        n = sum(min(len(l), _cupo(t, min(args.minimo, t), ya[o])) for o, l in candidatas.items())
        sim.append((t, n))

    campos = ["oportunidad_id", "oportunidad", "cliente", "anio", "situacion", "parte", "categoria", "ruta",
              "nombre", "file_id", "sha1", "kb", "tipo_probable", "enlace_box"]
    _csv(SELECCION_CSV, [{k: f.get(k, "") for k in campos} for f in elegidas])

    total_cand = sum(len(l) for l in candidatas.values())
    L = [f"SELECCIÓN DE FOTOS A ETIQUETAR · {ahora()} UTC", "",
         f"Pendientes de etiquetar: {len(pend)}",
         "Descartadas antes de elegir:"]
    for k, v in descartes.most_common():
        L.append(f"  {v:7d}  {k}")
    L += [f"Candidatas (fotos de campaña): {total_cand} en {len(candidatas)} oportunidades", "",
          f"SELECCIONADAS con tope {args.tope} por oportunidad (mínimo {args.minimo} si ya hay fotos en el archivo): "
          f"{len(elegidas)}",
          f"  coste orientativo con Haiku: ≈ {len(elegidas) * COSTE_FOTO:.2f} $", "",
          "Con otros topes:"]
    for t, n in sim:
        L.append(f"  tope {t:4d}: {n:7d} fotos · ≈ {n * COSTE_FOTO:6.2f} $")
    L += ["", "Por situación:"]
    for k, v in Counter(f["situacion"] or "(sin dato)" for f in elegidas).most_common():
        L.append(f"  {v:7d}  {k}")
    L += ["", "Por año:"]
    for k, v in sorted(Counter(str(f["anio"] or "(sin año)") for f in elegidas).items()):
        L.append(f"  {v:7d}  {k}")
    L += ["", "Carpetas excluidas (las 40 con más fotos: palabra que la excluye, carpeta y oportunidad):"]
    for (palabra, carpeta, op), v in carpetas_visit.most_common(40):
        L.append(f"  {v:7d}  '{palabra}' en «{carpeta}» · {op}")
    nombres = Counter(f["ruta"].split("/")[-1][:50] for l in candidatas.values() for f in l)
    L += ["", "Carpetas candidatas más grandes (las 30; si alguna es de visitantes, añádela a FOTOS_EXCLUIR_CARPETAS):"]
    for k, v in nombres.most_common(30):
        L.append(f"  {v:7d}  {k}")
    L += ["", "Detalle: seleccion_fotos.csv (una fila por foto elegida).", "No se ha enviado nada a la IA."]
    SELECCION_TXT.write_text("\n".join(L) + "\n", "utf-8")
    print(SELECCION_TXT.read_text("utf-8"))


def cmd_todo(args):
    if args.forzar or not ARCHIVO_JSON.exists():
        cmd_archivo(args)
    cmd_huellas(args)
    cmd_informe(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("orden", choices=["archivo", "huellas", "informe", "todo", "seleccionar"])
    ap.add_argument("--forzar", action="store_true", help="vuelve a leerlo todo de Box")
    ap.add_argument("--tope", type=int, default=int(os.getenv("FOTOS_TOPE", "50")),
                    help="seleccionar: máximo de fotos por oportunidad (50)")
    ap.add_argument("--minimo", type=int, default=int(os.getenv("FOTOS_MINIMO", "10")),
                    help="seleccionar: fotos aunque la oportunidad ya tenga muchas en el archivo (10)")
    ap.add_argument("--con-propuesta", action="store_true",
                    help="seleccionar: incluye también las imágenes de la propuesta comercial")
    args = ap.parse_args()
    DIR_FOTOS.mkdir(parents=True, exist_ok=True)
    {"archivo": cmd_archivo, "huellas": cmd_huellas, "informe": cmd_informe, "todo": cmd_todo,
     "seleccionar": cmd_seleccionar}[args.orden](args)


if __name__ == "__main__":
    main()

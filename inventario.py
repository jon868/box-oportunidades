#!/usr/bin/env python3
"""
Inventario de oportunidades · fase 1 (sin IA, sin coste de API de Claude)
alegria-activity

Cruza los negocios de HubSpot (fuente de verdad del estado: ganada, perdida, importe, fecha…)
con las carpetas de Box / 0.Salesforce / Oportunidades (fuente de verdad de la documentación).

Uso:
  python inventario.py hubspot          Descarga todos los negocios (datos/hubspot_negocios.json)
  python inventario.py box [--forzar]   Recorre Oportunidades en Box (datos/box_oportunidades.json); reanuda
  python inventario.py cruzar           Une ambos y genera inventario.json/.csv, huecos.csv y resumen.txt
  python inventario.py todo             Las tres cosas seguidas

Solo LEE de Box y de HubSpot. No escribe, mueve ni borra nada en ninguno de los dos.
"""
import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

VERSION = "v1"

# ---------------------------------------------------------------- configuración
BOX_API = "https://api.box.com/2.0"
HS_API = "https://api.hubapi.com"
CARPETA_RAIZ = os.getenv("BOX_FOLDER_ID", "3046666597")  # 0.Salesforce / Oportunidades
HILOS = int(os.getenv("HILOS", "6"))
DATA = Path(os.getenv("DATA_DIR", "datos"))

HS_JSON = DATA / "hubspot_negocios.json"
BOX_JSON = DATA / "box_oportunidades.json"
INV_JSON = DATA / "inventario.json"
INV_CSV = DATA / "inventario.csv"
HUECOS_CSV = DATA / "huecos.csv"
SIN_CARPETA_CSV = DATA / "negocios_sin_carpeta.csv"
RESUMEN = DATA / "resumen.txt"

# Carpetas de primer nivel que no son países ni contienen oportunidades
NO_PAISES = {x.strip() for x in os.getenv("EXCLUIR_PAISES", "").split(",") if x.strip()}

HS_PROPS = ["dealname", "dealstage", "pipeline", "amount", "closedate", "createdate",
            "hubspot_owner_id", "box_opp_folderid", "id_box_carpeta_presupuesto"]

# Estado del negocio a partir de la etapa (ids internos de HubSpot; las desconocidas → "en curso")
ETAPA_GRUPO = {"closedwon": "ganada", "closedlost": "perdida", "5771971789": "dormida"}

# ------------------------------------------------- plantilla de carpetas → categoría y sensibilidad
# Sensibilidad: general (todos), economico (presupuestos, contratos, facturas), personal (datos del equipo)
PLANTILLA = {
    "propuesta comercial": {
        "dossier": ("dossier", "general"),
        "presupuestos": ("presupuesto", "economico"),
        "pliegos concurso": ("pliegos", "general"),
        "disenos": ("diseños de propuesta", "general"),
        "planos e info tecnica": ("planos e info técnica", "general"),
        "info cliente": ("info del cliente", "general"),
        "contenidos y documentacion": ("contenidos", "general"),
        "otros": ("otros", "general"),
    },
    "proyecto": {
        "contrato": ("contrato", "economico"),
        "personal": ("personal", "personal"),
        "fotos y videos": ("fotos y vídeos", "general"),
        "disenos": ("diseños", "general"),
        "mobiliarios distribucion planos": ("mobiliario y planos", "general"),
        "sectec": ("secretaría técnica", "general"),  # permisos, rutas, convocatorias, asistencia
        "contenidos": ("contenidos", "general"),
        "otra documentacion": ("otra documentación", "general"),
        "informatica": ("informática", "general"),
    },
}
# Palabras en el nombre del archivo que suben la sensibilidad aunque esté en otra carpeta
PALABRAS_ECONOMICO = ["presupuesto", "ppto", "factura", "contrato", "oferta economica", "tarifa",
                      "escandallo", "coste", "costes", "precio", "pedido", "albaran", "aval", "garantia definitiva"]
PALABRAS_PERSONAL = ["nomina", "dni", "nie", "seguridad social", "ta2", "tc2", "idc", "curriculum", "cv",
                     "pasaporte", "alta trabajador", "baja trabajador", "certificado delitos", "carne conducir"]

GRUPOS_EXT = {
    "documento": {"pdf", "doc", "docx", "odt", "rtf", "txt", "pages", "md"},
    "hoja de cálculo": {"xls", "xlsx", "xlsm", "csv", "ods", "numbers"},
    "presentación": {"ppt", "pptx", "key", "odp"},
    "imagen": {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "tif", "tiff", "bmp"},
    "vídeo": {"mp4", "mov", "avi", "mkv", "m4v", "wmv", "mts", "mpg", "mpeg"},
    "diseño": {"ai", "psd", "indd", "eps", "svg", "cdr", "idml", "sketch", "fig", "xd"},
    "plano/3D": {"dwg", "dxf", "skp", "3ds", "max", "obj", "stl", "step", "stp", "ifc", "rvt"},
    "comprimido": {"zip", "rar", "7z", "gz", "tar"},
    "correo": {"msg", "eml"},
}
EXT_GRUPO = {e: g for g, exts in GRUPOS_EXT.items() for e in exts}

_lock = threading.Lock()
_token = {"v": None, "exp": 0}


# ---------------------------------------------------------------- utilidades
def entorno(nombre):
    valor = os.getenv(nombre)
    if not valor:
        sys.exit(f"Falta la variable de entorno {nombre} (mira .env.example).")
    return valor


def normal(texto):
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def leer_json(p, defecto=None):
    return json.loads(p.read_text("utf-8")) if p.exists() else defecto


def escribir_json(p, datos):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def anio_de(fecha):
    m = re.match(r"(\d{4})", str(fecha or ""))
    return int(m.group(1)) if m else None


def mb(b):
    return round((b or 0) / 1024 / 1024, 1)


# ---------------------------------------------------------------- Box
def box_token():
    if _token["v"] and time.time() < _token["exp"] - 60:
        return _token["v"]
    with _lock:
        if _token["v"] and time.time() < _token["exp"] - 60:
            return _token["v"]
        r = requests.post("https://api.box.com/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": entorno("BOX_CLIENT_ID"),
            "client_secret": entorno("BOX_CLIENT_SECRET"),
            "box_subject_type": os.getenv("BOX_SUBJECT_TYPE", "enterprise"),
            "box_subject_id": entorno("BOX_SUBJECT_ID"),
        }, timeout=30)
        if r.status_code != 200:
            sys.exit(f"Box no ha dado token ({r.status_code}): {r.text[:300]}")
        j = r.json()
        _token.update(v=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
        return _token["v"]


def box_get(ruta, params=None):
    r = None
    for intento in range(8):
        r = requests.get(f"{BOX_API}{ruta}", params=params,
                         headers={"Authorization": f"Bearer {box_token()}"}, timeout=120)
        if r.status_code == 401:
            _token["v"] = None
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 2 ** intento)))
            continue
        return r
    return r


def box_items(folder_id):
    """Todos los elementos de una carpeta (pagina de 1000 en 1000)."""
    salida, offset = [], 0
    while True:
        r = box_get(f"/folders/{folder_id}/items",
                    {"fields": "id,type,name,size,extension,modified_at,created_at", "limit": 1000, "offset": offset})
        r.raise_for_status()
        j = r.json()
        salida += j["entries"]
        offset += len(j["entries"])
        if not j["entries"] or offset >= j.get("total_count", 0):
            return salida


def box_carpeta(folder_id):
    r = box_get(f"/folders/{folder_id}", {"fields": "id,name,size,modified_at,created_at,path_collection"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = r.json()
    ruta = " / ".join(e["name"] for e in j.get("path_collection", {}).get("entries", []))
    return {"id": j["id"], "nombre": j["name"], "size": j.get("size", 0),
            "modificada": j.get("modified_at"), "creada": j.get("created_at"), "ruta": ruta}


def _contiene(texto_normal, palabras):
    """Coincidencia por inicio de palabra, para que 'dni' no salte dentro de otra palabra."""
    return any(re.search(rf"(?:^| ){re.escape(normal(p))}(?:$| |s )", f" {texto_normal} ") for p in palabras)


def clasificar(parte, sub, nombre):
    """(categoría, sensibilidad) de un archivo según la carpeta de plantilla y su nombre."""
    cat, sens = "sin clasificar", "general"
    tabla = PLANTILLA.get(normal(parte))
    ns = normal(sub)
    if tabla is None:                                               # carpeta de primer nivel fuera de plantilla
        cat = parte if parte != "(raíz)" else "sin clasificar"
    elif ns in tabla:
        cat, sens = tabla[ns]
    elif ns.startswith("sectec") and "sectec" in tabla:           # "SecTec AFARABA", etc.
        cat, sens = tabla["sectec"]
    elif sub:
        cat = sub                                                   # subcarpeta fuera de plantilla
    n = normal(nombre)
    if sens != "personal" and _contiene(n, PALABRAS_PERSONAL):
        sens = "personal"
    elif sens == "general" and _contiene(n, PALABRAS_ECONOMICO):
        sens = "economico"
    return cat, sens


def recorrer(folder_id, ruta, archivos, carpetas_vacias, aviso=None):
    """Lista recursivamente; no entra en carpetas que Box dice que pesan 0."""
    for it in box_items(folder_id):
        sub = f"{ruta}/{it['name']}" if ruta else it["name"]
        if it["type"] == "folder":
            if it.get("size", 0) > 0:
                if aviso:
                    aviso["carpetas"] += 1
                    if aviso["carpetas"] % 200 == 0:
                        print(f"    … {aviso['nombre'][:60]}: {aviso['carpetas']} subcarpetas, "
                              f"{len(archivos)} archivos leídos", flush=True)
                recorrer(it["id"], sub, archivos, carpetas_vacias, aviso)
            else:
                carpetas_vacias.append(sub)
        elif it["type"] == "file" and not it["name"].startswith("._"):   # restos de Mac
            archivos.append({"id": it["id"], "nombre": it["name"], "ruta": ruta,
                             "ext": (it.get("extension") or "").lower(), "size": it.get("size", 0),
                             "modificado": (it.get("modified_at") or "")[:10]})


def ficha_oportunidad(pais, carpeta):
    """Recorre una oportunidad y resume qué hay en cada parte."""
    archivos, vacias = [], []
    if carpeta.get("size", 0) > 0:
        grande = carpeta.get("size", 0) > 5 * 1024 ** 3      # > 5 GB: avisa en el log mientras la lee
        if grande:
            print(f"    empiezo {carpeta['name'][:60]} ({mb(carpeta['size']) / 1024:.0f} GB)", flush=True)
        recorrer(carpeta["id"], "", archivos, vacias, {"nombre": carpeta["name"], "carpetas": 0} if grande else None)
    partes = defaultdict(lambda: {"archivos": 0, "mb": 0.0, "subcarpetas": Counter()})
    for a in archivos:
        trozos = a["ruta"].split("/") if a["ruta"] else []
        parte = trozos[0] if trozos else "(raíz)"
        sub = trozos[1] if len(trozos) > 1 else ""
        clave = normal(parte)
        parte_n = ("propuesta" if clave == "propuesta comercial" else "proyecto" if clave == "proyecto" else "fuera de plantilla")
        a["parte"] = parte_n
        a["categoria"], a["sensibilidad"] = clasificar(parte, sub, a["nombre"])
        a["tipo"] = EXT_GRUPO.get(a["ext"], "otro")
        partes[parte_n]["archivos"] += 1
        partes[parte_n]["mb"] += a["size"] / 1024 / 1024
        partes[parte_n]["subcarpetas"][a["categoria"]] += 1
    return {
        "id": carpeta["id"], "nombre": carpeta["name"], "pais": pais,
        "size": carpeta.get("size", 0), "modificada": carpeta.get("modified_at"), "creada": carpeta.get("created_at"),
        "partes": {k: {"archivos": v["archivos"], "mb": round(v["mb"], 1), "por_categoria": dict(v["subcarpetas"])}
                   for k, v in partes.items()},
        "archivos": archivos,
        "leida": ahora(),
    }


def cmd_box(args):
    previo = leer_json(BOX_JSON, {}) or {}
    cache = {o["id"]: o for o in previo.get("oportunidades", [])}
    paises = [p for p in box_items(CARPETA_RAIZ) if p["type"] == "folder" and p["name"] not in NO_PAISES]
    tareas = []
    sueltos = []
    for p in paises:
        hijos = box_items(p["id"])
        for h in hijos:
            if h["type"] == "folder":
                tareas.append((p["name"], h))
            else:
                sueltos.append({"pais": p["name"], "id": h["id"], "nombre": h["name"], "size": h.get("size", 0)})
        print(f"  {p['name']}: {sum(1 for h in hijos if h['type'] == 'folder')} oportunidades")
    print(f"Total: {len(tareas)} carpetas de oportunidad")

    reutilizadas, leidas, errores = 0, 0, []
    resultado = []

    def trabajo(pais, h):
        c = cache.get(h["id"])
        if (c and not args.forzar and c.get("modificada") == h.get("modified_at")
                and c.get("size") == h.get("size", 0) and c.get("nombre") == h["name"]):
            return c, True
        return ficha_oportunidad(pais, h), False

    with ThreadPoolExecutor(HILOS) as ex:
        futuros = {ex.submit(trabajo, pais, h): (pais, h) for pais, h in tareas}
        for i, f in enumerate(as_completed(futuros), 1):
            pais, h = futuros[f]
            try:
                ficha, reuso = f.result()
                resultado.append(ficha)
                reutilizadas += reuso
                leidas += not reuso
            except Exception as e:  # noqa: BLE001 — seguimos con el resto
                errores.append({"id": h["id"], "nombre": h["name"], "error": str(e)[:300]})
            if i % 100 == 0:
                print(f"  {i}/{len(tareas)} · leídas {leidas} · reutilizadas {reutilizadas} · errores {len(errores)}")
                hechas = {r["id"] for r in resultado}
                escribir_json(BOX_JSON, {"generado": ahora(), "parcial": True, "errores": errores,
                                         "oportunidades": resultado + [o for k, o in cache.items() if k not in hechas]})

    escribir_json(BOX_JSON, {"generado": ahora(), "raiz": CARPETA_RAIZ, "oportunidades": resultado,
                             "archivos_sueltos": sueltos, "errores": errores})
    print(f"Box: {len(resultado)} oportunidades ({leidas} leídas, {reutilizadas} sin cambios), "
          f"{sum(len(o['archivos']) for o in resultado)} archivos, {len(errores)} errores → {BOX_JSON}")


# ---------------------------------------------------------------- HubSpot
def hs_get(ruta, params=None):
    tok = entorno("HUBSPOT_TOKEN")
    r = None
    for intento in range(8):
        r = requests.get(f"{HS_API}{ruta}", params=params, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 2 ** intento)))
            continue
        return r
    return r


def hs_post(ruta, cuerpo):
    tok = entorno("HUBSPOT_TOKEN")
    r = None
    for intento in range(8):
        r = requests.post(f"{HS_API}{ruta}", json=cuerpo, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 2 ** intento)))
            continue
        return r
    return r


def cmd_hubspot(_args):
    # Etapas: id → nombre visible
    etapas = {}
    r = hs_get("/crm/v3/pipelines/deals")
    r.raise_for_status()
    for p in r.json().get("results", []):
        for s in p.get("stages", []):
            etapas[s["id"]] = s["label"]

    negocios, after = [], None
    while True:
        params = {"limit": 100, "properties": ",".join(HS_PROPS), "associations": "companies", "archived": "false"}
        if after:
            params["after"] = after
        r = hs_get("/crm/v3/objects/deals", params)
        r.raise_for_status()
        j = r.json()
        for d in j.get("results", []):
            p = d.get("properties", {})
            emp = [a["id"] for a in d.get("associations", {}).get("companies", {}).get("results", [])]
            negocios.append({"id": d["id"], **{k: p.get(k) for k in HS_PROPS}, "empresas_ids": emp})
        after = j.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        if len(negocios) % 1000 == 0:
            print(f"  {len(negocios)} negocios…")

    # Nombres de empresas asociadas (lote de 100)
    ids = sorted({e for n in negocios for e in n["empresas_ids"]})
    empresas = {}
    for i in range(0, len(ids), 100):
        r = hs_post("/crm/v3/objects/companies/batch/read",
                    {"properties": ["name"], "inputs": [{"id": x} for x in ids[i:i + 100]]})
        if r.status_code == 403:
            print("  (sin permiso para leer empresas: se usa el nombre del negocio)")
            break
        r.raise_for_status()
        for c in r.json().get("results", []):
            empresas[c["id"]] = c.get("properties", {}).get("name")

    # Propietarios (comerciales)
    propietarios = {}
    r = hs_get("/crm/v3/owners", {"limit": 500})
    if r.status_code == 200:
        for o in r.json().get("results", []):
            propietarios[str(o["id"])] = " ".join(x for x in [o.get("firstName"), o.get("lastName")] if x) or o.get("email")
    else:
        print("  (sin permiso para leer propietarios: no saldrá el comercial)")

    for n in negocios:
        n["etapa"] = etapas.get(n["dealstage"], n["dealstage"])
        n["empresas"] = [empresas[e] for e in n["empresas_ids"] if empresas.get(e)]
        n["comercial"] = propietarios.get(str(n.get("hubspot_owner_id") or ""))
    escribir_json(HS_JSON, {"generado": ahora(), "etapas": etapas, "negocios": negocios})
    print(f"HubSpot: {len(negocios)} negocios, {sum(1 for n in negocios if n.get('box_opp_folderid'))} "
          f"con carpeta de Box → {HS_JSON}")


# ---------------------------------------------------------------- cruce
def grupo_etapa(n):
    return ETAPA_GRUPO.get(n.get("dealstage"), "en curso") if n else None


def nombre_base(nombre):
    """Nombre sin el ID de HubSpot del principio: '5192… - ABAI GROUP_X' y 'ABAI GROUP_X' son gemelas."""
    return normal(re.sub(r"^\s*\d{9,14}\s*-\s*", "", nombre or ""))


def principal(negocios):
    """Si una carpeta tiene varios negocios: el ganado; si no, el de cierre más reciente."""
    if not negocios:
        return None
    return sorted(negocios, key=lambda n: (n.get("dealstage") == "closedwon", n.get("closedate") or ""))[-1]


def situacion(op, neg):
    prop = op["partes"].get("propuesta", {}).get("archivos", 0) + op["partes"].get("fuera de plantilla", {}).get("archivos", 0)
    proy = op["partes"].get("proyecto", {}).get("archivos", 0)
    g = grupo_etapa(neg)
    if g == "ganada":
        if proy:
            return "ganada documentada"
        return "ganada solo con propuesta" if prop else "ganada SIN documentación"
    if g in ("perdida", "dormida"):
        return f"{g} con propuesta" if (prop or proy) else f"{g} sin documentación"
    if g == "en curso":
        return "en curso con documentación" if (prop or proy) else "en curso sin documentación"
    # Sin negocio en HubSpot: solo sabemos lo que hay en Box
    if proy:
        return "sin negocio · con proyecto"
    if prop:
        return "sin negocio · con propuesta"
    return "sin negocio · vacía"


def cmd_cruzar(_args):
    hs = leer_json(HS_JSON)
    bx = leer_json(BOX_JSON)
    if not hs or not bx:
        sys.exit("Faltan datos: ejecuta antes 'hubspot' y 'box'.")
    negocios = hs["negocios"]
    ops = bx["oportunidades"]
    por_folder = defaultdict(list)
    por_nombre = defaultdict(list)
    por_id = {n["id"]: n for n in negocios}
    for n in negocios:
        if n.get("box_opp_folderid"):
            por_folder[str(n["box_opp_folderid"]).strip()].append(n)
        por_nombre[normal(n.get("dealname"))].append(n)

    # 1ª pasada: cruce directo de cada carpeta con su(s) negocio(s)
    usados = set()
    cruces = {}
    for op in ops:
        metodo, cand = None, por_folder.get(op["id"], [])
        if cand:
            metodo = "id de carpeta"
        if not cand:
            m = re.match(r"\s*(\d{9,14})\s*-", op["nombre"])
            if m and m.group(1) in por_id:
                cand, metodo = [por_id[m.group(1)]], "id en el nombre"
        if not cand:
            c = [n for n in por_nombre.get(nombre_base(op["nombre"]), []) if not n.get("box_opp_folderid")]
            if c:
                cand, metodo = c, "nombre"
        cruces[op["id"]] = (cand, metodo)
        usados.update(n["id"] for n in cand)

    # 2ª pasada: carpetas gemelas (la de la época de Salesforce y la que crea HubSpot con el ID delante).
    # Si una no tiene negocio y su gemela sí, hereda el de la gemela.
    gemelas = defaultdict(list)
    for op in ops:
        gemelas[(op["pais"], nombre_base(op["nombre"]))].append(op["id"])
    for op in ops:
        cand, metodo = cruces[op["id"]]
        if cand:
            continue
        for otra in gemelas[(op["pais"], nombre_base(op["nombre"]))]:
            if otra != op["id"] and cruces[otra][0]:
                cruces[op["id"]] = (cruces[otra][0], "carpeta gemela")
                break

    filas = []
    for op in ops:
        cand, metodo = cruces[op["id"]]
        neg = principal(cand)
        sit = situacion(op, neg)
        tipos = Counter(a["tipo"] for a in op["archivos"])
        sens = Counter(a["sensibilidad"] for a in op["archivos"])
        filas.append({
            "carpeta_id": op["id"], "carpeta": op["nombre"], "pais": op["pais"],
            "enlace_box": f"https://app.box.com/folder/{op['id']}",
            "situacion": sit,
            "carpetas_gemelas": " ".join(x for x in gemelas[(op["pais"], nombre_base(op["nombre"]))] if x != op["id"]),
            "negocio_id": neg["id"] if neg else "", "negocio": neg.get("dealname") if neg else "",
            "etapa": neg.get("etapa") if neg else "", "importe": neg.get("amount") if neg else "",
            "fecha_cierre": (neg.get("closedate") or "")[:10] if neg else "",
            "anio": anio_de(neg.get("closedate")) if neg else anio_de(op.get("creada")),
            "cliente": ", ".join(neg.get("empresas") or []) if neg else "",
            "comercial": (neg.get("comercial") or "") if neg else "",
            "otros_negocios": "; ".join(f"{n['id']} {n.get('etapa')}" for n in cand if n is not neg),
            "cruce": metodo or "",
            "archivos_propuesta": op["partes"].get("propuesta", {}).get("archivos", 0),
            "archivos_proyecto": op["partes"].get("proyecto", {}).get("archivos", 0),
            "archivos_fuera_plantilla": op["partes"].get("fuera de plantilla", {}).get("archivos", 0),
            "mb": mb(op.get("size")),
            "categorias_propuesta": "; ".join(f"{k} ({v})" for k, v in
                                              op["partes"].get("propuesta", {}).get("por_categoria", {}).items()),
            "categorias_proyecto": "; ".join(f"{k} ({v})" for k, v in
                                             op["partes"].get("proyecto", {}).get("por_categoria", {}).items()),
            "tipos": "; ".join(f"{k} ({v})" for k, v in tipos.most_common()),
            "archivos_economicos": sens.get("economico", 0),
            "archivos_personales": sens.get("personal", 0),
        })

    # Negocios cuya carpeta no está en el árbol de Oportunidades
    fuera = [n for n in negocios if n["id"] not in usados]
    con_carpeta_fuera = [n for n in fuera if n.get("box_opp_folderid")]
    print(f"Comprobando {len(con_carpeta_fuera)} carpetas de negocios que no están en Oportunidades…")
    info_fuera = {}

    def mirar(n):
        try:
            return n["id"], box_carpeta(str(n["box_opp_folderid"]).strip())
        except Exception as e:  # noqa: BLE001
            return n["id"], {"error": str(e)[:200]}

    with ThreadPoolExecutor(HILOS) as ex:
        for nid, info in ex.map(mirar, con_carpeta_fuera):
            info_fuera[nid] = info

    sin_carpeta = []
    for n in fuera:
        info = info_fuera.get(n["id"])
        if n.get("box_opp_folderid"):
            if info is None:
                estado = "carpeta borrada o sin acceso"
            elif info.get("error"):
                estado = f"error: {info['error']}"
            else:
                estado = f"carpeta fuera de Oportunidades ({mb(info['size'])} MB)"
        else:
            estado = "sin carpeta en Box"
        sin_carpeta.append({
            "negocio_id": n["id"], "negocio": n.get("dealname"), "etapa": n.get("etapa"),
            "grupo": grupo_etapa(n), "importe": n.get("amount"), "fecha_cierre": (n.get("closedate") or "")[:10],
            "cliente": ", ".join(n.get("empresas") or []), "comercial": n.get("comercial") or "",
            "estado_carpeta": estado,
            "carpeta_id": n.get("box_opp_folderid") or "",
            "carpeta_ruta": f"{info['ruta']} / {info['nombre']}" if info and not info.get("error") else "",
        })

    # Salidas
    escribir_json(INV_JSON, {"generado": ahora(), "version": VERSION, "oportunidades": filas,
                             "negocios_sin_carpeta": sin_carpeta})
    _csv(INV_CSV, filas)
    importe = lambda f: float(f.get("importe") or 0)  # noqa: E731
    # Un negocio es hueco si NINGUNA de sus carpetas tiene proyecto (puede estar en la gemela)
    proyecto_por_negocio = defaultdict(int)
    for f in filas:
        if f["negocio_id"]:
            proyecto_por_negocio[f["negocio_id"]] += f["archivos_proyecto"]
    huecos = sorted([f for f in filas if f["situacion"] in ("ganada SIN documentación", "ganada solo con propuesta")
                     and not proyecto_por_negocio[f["negocio_id"]]],
                    key=importe, reverse=True)
    huecos += sorted([{**s, "situacion": "ganada " + s["estado_carpeta"]} for s in sin_carpeta
                      if s["grupo"] == "ganada" and not s["estado_carpeta"].startswith("carpeta fuera")],
                     key=importe, reverse=True)
    _csv(HUECOS_CSV, huecos)
    _csv(SIN_CARPETA_CSV, sin_carpeta)
    _resumen(filas, sin_carpeta, hs, bx)
    print(RESUMEN.read_text("utf-8"))


def _csv(p, filas):
    p.parent.mkdir(parents=True, exist_ok=True)
    if not filas:
        p.write_text("", "utf-8")
        return
    campos = list(dict.fromkeys(k for f in filas for k in f))
    with p.open("w", newline="", encoding="utf-8-sig") as fh:   # utf-8-sig: Excel abre bien los acentos
        w = csv.DictWriter(fh, fieldnames=campos, delimiter=";")
        w.writeheader()
        w.writerows(filas)


def _resumen(filas, sin_carpeta, hs, bx):
    sit = Counter(f["situacion"] for f in filas)
    cruce = Counter(f["cruce"] or "sin negocio" for f in filas)
    pais = Counter(f["pais"] for f in filas if not f["situacion"].endswith("vacía"))
    fuera = Counter(s["estado_carpeta"].split(" (")[0] for s in sin_carpeta)
    fuera_g = Counter(s["estado_carpeta"].split(" (")[0] for s in sin_carpeta if s["grupo"] == "ganada")
    tot_arch = sum(len(o["archivos"]) for o in bx["oportunidades"])
    tipos = Counter(a["tipo"] for o in bx["oportunidades"] for a in o["archivos"])
    sens = Counter(a["sensibilidad"] for o in bx["oportunidades"] for a in o["archivos"])
    docs = sum(1 for o in bx["oportunidades"] for a in o["archivos"]
               if a["tipo"] in ("documento", "hoja de cálculo", "presentación") and a["sensibilidad"] != "personal")
    L = [f"INVENTARIO DE OPORTUNIDADES · {ahora()} UTC", "",
         f"Box: {len(filas)} carpetas de oportunidad, {tot_arch} archivos · HubSpot: {len(hs['negocios'])} negocios", "",
         "SITUACIÓN DE CADA CARPETA"]
    L += [f"  {v:>5}  {k}" for k, v in sit.most_common()]
    L += ["", "CÓMO SE HA CRUZADO CON HUBSPOT"] + [f"  {v:>5}  {k}" for k, v in cruce.most_common()]
    n_gem = sum(1 for f in filas if f["carpetas_gemelas"])
    L += ["", f"CARPETAS CON GEMELA (misma oportunidad en la carpeta antigua y en la creada por HubSpot): {n_gem}"]
    L += ["", "NEGOCIOS DE HUBSPOT SIN CARPETA EN OPORTUNIDADES (todos / ganados)"]
    L += [f"  {v:>5} / {fuera_g.get(k, 0):>4}  {k}" for k, v in fuera.most_common()]
    L += ["", "CARPETAS CON CONTENIDO POR PAÍS"] + [f"  {v:>5}  {k}" for k, v in pais.most_common()]
    L += ["", "ARCHIVOS POR TIPO"] + [f"  {v:>7}  {k}" for k, v in tipos.most_common()]
    L += ["", "ARCHIVOS POR SENSIBILIDAD"] + [f"  {v:>7}  {k}" for k, v in sens.most_common()]
    L += ["", f"Documentos legibles (PDF, Word, Excel, PowerPoint; sin 'personal') para la fase 2: {docs}"]
    if bx.get("errores"):
        L += ["", f"ERRORES DE LECTURA EN BOX: {len(bx['errores'])} (ver box_oportunidades.json)"]
    RESUMEN.write_text("\n".join(L) + "\n", "utf-8")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Inventario de oportunidades (Box + HubSpot)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hubspot")
    b = sub.add_parser("box")
    b.add_argument("--forzar", action="store_true", help="vuelve a leer todas las carpetas aunque no hayan cambiado")
    sub.add_parser("cruzar")
    t = sub.add_parser("todo")
    t.add_argument("--forzar", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    if args.cmd == "hubspot":
        cmd_hubspot(args)
    elif args.cmd == "box":
        cmd_box(args)
    elif args.cmd == "cruzar":
        cmd_cruzar(args)
    elif args.cmd == "todo":
        cmd_hubspot(args)
        cmd_box(args)
        cmd_cruzar(args)


if __name__ == "__main__":
    main()

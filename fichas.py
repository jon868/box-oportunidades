#!/usr/bin/env python3
"""
Fichas de oportunidades con Haiku (Batch API) · fases 2 y 3
alegria-activity

Necesita el inventario hecho (inventario.py box + cruzar). Corre en el servidor de Easypanel con la clave de API
de Anthropic: no gasta nada de Claude Team.

  python fichas.py seleccionar --piloto     Elige las oportunidades del piloto (o --todas)
  python fichas.py propuestas [--si]        Fase 2: lee dossier, presupuesto y pliegos → ficha "qué se presentó"
  python fichas.py mapas [--si]             Fase 3a: árbol de carpetas de cada proyecto ganado → mapa y documentos clave
  python fichas.py proyectos [--si]         Fase 3b: lee los documentos clave → ficha del proyecto
  python fichas.py piloto [--si]            Las tres seguidas, esperando a que acabe cada lote
  python fichas.py estado                   Estado de los lotes enviados
  python fichas.py seguir <fase>            Espera y recoge un lote ya enviado (si se cortó la consola)
  python fichas.py indice                   Junta todo en fichas/indice_oportunidades.json y fichas/revision.csv
  python fichas.py actualizar               Actualización incremental (la que se lanza cada noche): HubSpot + Box,
                                            fichas solo de lo nuevo o cambiado, e índice

Fases: propuestas, mapas, proyectos. Cada una enseña el coste estimado y pide confirmación (salvo --si).
Solo LEE de Box y de los datos del inventario. No escribe, mueve ni borra nada en Box.
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

import inventario as inv

VERSION = "v5"
MODELO = os.getenv("MODELO", "claude-haiku-5-5")
# USD por millón de tokens en Batch (entrada, salida). Orientativos, los mismos del etiquetado de fotos: revísalos en la Console.
PRECIO_ENTRADA = float(os.getenv("PRECIO_ENTRADA", "0.05"))
PRECIO_SALIDA = float(os.getenv("PRECIO_SALIDA", "0.25"))
HILOS = int(os.getenv("HILOS", "6"))

DATA = inv.DATA
F = DATA / "fichas"
TEXTOS = F / "textos"
SELECCION = F / "seleccion.json"
LOTES = F / "lotes.json"
INDICE = F / "indice_oportunidades.json"
REVISION = F / "revision.csv"
HUELLAS = F / "huellas.json"
CERROJO = DATA / ".actualizando"
MAX_NOCTURNO = int(os.getenv("MAX_FICHAS_NOCHE", "300"))

LEGIBLES = ("documento", "hoja de cálculo", "presentación")
TEXTO_PLANO = {"txt", "csv", "md", "rtf"}

FLOTA = ["13,80", "Airstream", "CajaMar", "Camión rígido", "Cotazero", "Furgón", "Plataformas", "Semirremolque", "Set TV"]
# Cómo reconocer la flota en un documento (la rotulación cambia en cada campaña)
GUIA_FLOTA = """Flota propia de alegria-activity (se alquila con la campaña; NO es "fabricada a medida"):
- 13,80: autocar o unidad de 13,80 m con lateral desplegable / laterales extraíbles (módulo saliente). Existe desde 2006.
- Semirremolque: tráiler (cabeza tractora + semirremolque) expositivo de gran tamaño con laterales expandibles.
- Camión rígido: camión rígido con caja expositiva de laterales acristalados.
- Furgón: furgoneta grande con carrocería tipo caja, puerta lateral, escalera y toldo.
- Airstream: remolque/caravana compacta que se abre por un lateral como escaparate, con rampa.
- CajaMar: camión carrozado con escalera y rampa de acceso lateral (antigua oficina bancaria móvil).
- Plataformas: módulo-remolque grande tipo sala, con ventanas.
- Set TV: módulo plató de televisión con terraza superior.
- Cotazero.
Antes hubo vehículos de 12 m y de 15 m que ya no existen: si el documento usa uno de esos para una campaña, pon
"flota antigua (12 m)" o "flota antigua (15 m)". Una unidad de 12-14 m que no se despliega es un autobús.
Cómo decidir, en este orden:
1. Si lo que se vende o suministra es el vehículo (venta, concurso de suministro, fabricación para el cliente):
   "unidad fabricada a medida" (aunque sea un semirremolque o mida 13,80 m).
2. Si es una campaña o alquiler y la descripción encaja con la flota, usa el nombre de la flota. Reglas seguras:
   cualquier unidad de 13,80 m (o 13,8 / 13,85 m) con laterales extraíbles o desplegables → "13,80";
   cabeza tractora + semirremolque expositivo con laterales expandibles o extraíbles → "Semirremolque".
3. Solo si la campaña usa algo que claramente NO es nada de lo anterior (otras medidas u otra estructura, p. ej. un
   camión expositor de 10,47 m), pon "otro vehículo (no de la flota actual): <descripción breve>".
4. Si el documento no describe el vehículo, deja la lista vacía."""

OPCIONES_VEHICULO = FLOTA + ["flota antigua (12 m)", "flota antigua (15 m)", "unidad fabricada a medida",
                             "otro vehículo (no de la flota actual)", "sin unidad móvil", "no consta"]
CAMPOS_VEHICULO = {
    "vehiculo": {"type": "string", "enum": OPCIONES_VEHICULO,
                 "description": "Vehículo principal según la guía de flota"},
    "vehiculo_descripcion": {"type": "string", "description": "Cómo lo describe el documento (medidas, tipo, laterales)"},
}
MESES = r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|" \
        r"january|february|march|april|may|june|july|august|september|october|november|december|" \
        r"janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre|" \
        r"ene|feb|mar|abr|jun|jul|ago|sep|sept|oct|nov|dic"


def normalizar_vehiculo(vehiculo, descripcion, es_venta=False):
    """Reglas fijas por encima de lo que diga el modelo (Haiku no es constante con la flota)."""
    if isinstance(vehiculo, list):
        vehiculo = vehiculo[0] if vehiculo else None
    if isinstance(descripcion, list):
        descripcion = " ".join(map(str, descripcion))
    d = inv.normal(descripcion or "")
    if es_venta:
        return "unidad fabricada a medida"
    if vehiculo in ("sin unidad móvil",):
        return vehiculo
    if re.search(r"semirremolque|semi remolque|trailer|tractora", d) and re.search(r"lateral|expand|extraib|extensib", d):
        return "Semirremolque"
    if re.search(r"\b13 ?8\d?\b|13 8 m|1380", d) and re.search(r"lateral|desplieg|despleg|extraib|extensib|modulo", d):
        return "13,80"
    if re.search(r"\b12 m\b|\b12 metros\b", d) and vehiculo in ("otro vehículo (no de la flota actual)", "no consta"):
        return "flota antigua (12 m)"
    return vehiculo or "no consta"


LINEAS = ["Roadshow", "Acciones pedagógicas y sensibilización", "Marca y patrocinio",
          "Promoción y producto", "Manufacturing", "Agencia"]
SECCIONES = ["fotos", "vídeos", "diseño gráfico y vinilos", "planos y mobiliario", "ficha técnica del vehículo",
             "rutas y calendario", "permisos y licencias", "asistencia y registros de visitantes",
             "inscripciones y convocatorias", "informes y memorias", "contrato y facturación",
             "contenidos de la experiencia", "software e interactivos", "proveedores y compras",
             "comunicación y prensa", "formación del equipo", "otros"]

# Piloto acordado (fase 3): se buscan por nombre de carpeta
PILOTO_PROYECTOS = ["AFARABA_Campaña de test cognitivos 2022", "OMNICOM Techdata Tour Europa", "ISDIN_Roadshow 2025",
                    "Fundación Mapfre - Cámara", "INCIBE_Ciberseguridad Roadshow Escape Room"]
PILOTO_PROPUESTAS_FIJAS = ["AENA_Aeropuertos para ti"]
PILOTO_CUPOS = {"perdida": 10, "dormida": 6, "ganada": 8, "en curso": 3, "sin negocio": 3}


# ---------------------------------------------------------------- utilidades
def leer(p, defecto=None):
    return inv.leer_json(p, defecto)


def escribir(p, datos):
    inv.escribir_json(p, datos)


def archivos_de(carpeta_id):
    p = inv.DIR_CARPETAS / f"{carpeta_id}.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else []


def oportunidades():
    d = leer(inv.INV_JSON)
    if not d:
        sys.exit("Falta el inventario: ejecuta antes 'python inventario.py cruzar'.")
    return {f["carpeta_id"]: f for f in d["oportunidades"]}


def cliente_api():
    return anthropic.Anthropic(api_key=inv.entorno("ANTHROPIC_API_KEY"))


def coste(tokens_in, tokens_out):
    return tokens_in / 1e6 * PRECIO_ENTRADA + tokens_out / 1e6 * PRECIO_SALIDA


def confirmar(texto, args):
    print(texto)
    if getattr(args, "si", False):
        return
    if input("¿Seguir? (s/n) ").strip().lower() not in ("s", "si", "sí", "y"):
        sys.exit("Cancelado.")


def contexto_negocio(op):
    partes = [f"Carpeta: {op['carpeta']}", f"País: {op['pais']}"]
    if op.get("negocio"):
        partes.append(f"Negocio en HubSpot: {op['negocio']} · etapa {op['etapa']} · cierre {op['fecha_cierre']}"
                      + (f" · importe {op['importe']} €" if op.get("importe") else ""))
    if op.get("cliente"):
        partes.append(f"Empresa en HubSpot: {op['cliente']}")
    return "\n".join(partes)


# ---------------------------------------------------------------- texto de Box
def texto_box(file_id, ext, limite):
    """Texto de un archivo (caché en fichas/textos). Devuelve '' si Box no lo extrae."""
    cache = TEXTOS / f"{file_id}.txt"
    if cache.exists():
        return cache.read_text("utf-8")[:limite]
    texto = ""
    try:
        if ext in TEXTO_PLANO:
            r = inv.box_get(f"/files/{file_id}/content")
            if r.status_code == 200:
                texto = r.content[:2_000_000].decode("utf-8", "ignore")
        else:
            r = inv.requests.get(f"{inv.BOX_API}/files/{file_id}", params={"fields": "representations"},
                                 headers={"Authorization": f"Bearer {inv.box_token()}", "x-rep-hints": "[extracted_text]"},
                                 timeout=60)
            entradas = (r.json().get("representations") or {}).get("entries") or [] if r.status_code == 200 else []
            if entradas:
                rep = entradas[0]
                estado = rep.get("status", {}).get("state")
                plantilla = rep.get("content", {}).get("url_template")
                limite_t = time.time() + 90
                while estado in ("pending", "none") and time.time() < limite_t:
                    i = inv.requests.get(rep["info"]["url"], headers={"Authorization": f"Bearer {inv.box_token()}"},
                                         timeout=60).json()
                    estado = i.get("status", {}).get("state", estado)
                    plantilla = i.get("content", {}).get("url_template") or plantilla
                    if estado != "success":
                        time.sleep(3)
                if estado == "success" and plantilla:
                    t = inv.requests.get(plantilla.replace("{+asset_path}", ""),
                                         headers={"Authorization": f"Bearer {inv.box_token()}"}, timeout=120)
                    if t.status_code == 200:
                        texto = t.content.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 — un archivo que falla no para el resto
        print(f"    (sin texto de {file_id}: {str(e)[:120]})")
        return ""
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(texto, "utf-8")
    return texto[:limite]


def leer_textos(docs, limites):
    """docs: lista de archivos; limites: dict id → nº de caracteres. Descarga en paralelo."""
    with ThreadPoolExecutor(HILOS) as ex:
        textos = list(ex.map(lambda a: texto_box(a["id"], a["ext"], limites[a["id"]]), docs))
    return dict(zip([a["id"] for a in docs], textos))


# ---------------------------------------------------------------- lotes (Batch API)
def peticion(custom_id, system, contenido, herramienta, max_tokens):
    return {"custom_id": custom_id, "params": {
        "model": MODELO, "max_tokens": max_tokens, "system": system,
        "tools": [herramienta], "tool_choice": {"type": "tool", "name": herramienta["name"]},
        "messages": [{"role": "user", "content": contenido}]}}


def enviar(fase, peticiones, salida_estimada, args):
    if not peticiones:
        print(f"[{fase}] No hay nada que enviar.")
        return False
    t_in = sum(len(json.dumps(p["params"], ensure_ascii=False)) for p in peticiones) / 3.5
    t_out = salida_estimada * len(peticiones)
    confirmar(f"[{fase}] {len(peticiones)} peticiones a {MODELO} · ~{t_in / 1e6:.1f} M tokens de entrada · "
              f"coste estimado ~{coste(t_in, t_out):.2f} $ (orientativo)", args)
    cli = cliente_api()
    lotes = leer(LOTES, {}) or {}
    ids = []
    for i in range(0, len(peticiones), 2000):
        for intento in range(30):
            try:
                b = cli.messages.batches.create(requests=peticiones[i:i + 2000])
                break
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                print(f"  API ocupada ({type(e).__name__}); reintento en 1 min…")
                time.sleep(60)
        else:
            sys.exit("No se pudo crear el lote. Relanza más tarde.")
        ids.append(b.id)
        print(f"  Lote enviado: {b.id}")
    lotes[fase] = {"ids": ids, "recogido": False, "enviado": inv.ahora()}
    escribir(LOTES, lotes)
    return True


def esperar_y_recoger(fase):
    lotes = leer(LOTES, {}) or {}
    if fase not in lotes:
        sys.exit(f"No hay lote de '{fase}'.")
    cli = cliente_api()
    while True:
        acabados, cuentas = True, Counter()
        for lid in lotes[fase]["ids"]:
            b = cli.messages.batches.retrieve(lid)
            acabados &= b.processing_status == "ended"
            c = b.request_counts
            cuentas.update(ok=c.succeeded, error=c.errored, curso=c.processing, caducadas=c.expired)
        print(f"  [{fase}] {inv.ahora()} · ok {cuentas['ok']} · en curso {cuentas['curso']} · "
              f"error {cuentas['error']} · caducadas {cuentas['caducadas']}", flush=True)
        if acabados:
            break
        time.sleep(60)
    salida = F / f"resultados_{fase}.jsonl"
    previos = {}
    if salida.exists():
        for linea in salida.read_text("utf-8").splitlines():
            r = json.loads(linea)
            previos[r["custom_id"]] = r
    t_in = t_out = 0
    for lid in lotes[fase]["ids"]:
        for r in cli.messages.batches.results(lid):
            reg = {"custom_id": r.custom_id, "version": VERSION}
            if r.result.type == "succeeded":
                m = r.result.message
                t_in += m.usage.input_tokens
                t_out += m.usage.output_tokens
                bloque = next((b for b in m.content if b.type == "tool_use"), None)
                reg["ficha"] = bloque.input if bloque else None
                reg["fin"] = m.stop_reason
                if not bloque:
                    reg["error"] = "sin ficha"
                elif m.stop_reason == "max_tokens":
                    print(f"    ¡ficha cortada por longitud! {r.custom_id}")
            else:
                reg["error"] = r.result.type
            previos[r.custom_id] = reg
    with salida.open("w", encoding="utf-8") as fh:
        for r in previos.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    lotes[fase]["recogido"] = True
    lotes[fase]["coste_usd"] = round(coste(t_in, t_out), 4)
    escribir(LOTES, lotes)
    malos = sum(1 for r in previos.values() if r.get("error"))
    print(f"  [{fase}] recogidas {len(previos)} fichas ({malos} con error) · {t_in / 1e6:.2f} M tokens entrada, "
          f"{t_out / 1e3:.0f} k salida · ~{coste(t_in, t_out):.3f} $ → {salida}")


def resultados(fase):
    p = F / f"resultados_{fase}.jsonl"
    if not p.exists():
        return {}
    out = {}
    for linea in p.read_text("utf-8").splitlines():
        r = json.loads(linea)
        if r.get("ficha"):
            out[r["custom_id"].split("_", 1)[1]] = r["ficha"]
    return out


# ---------------------------------------------------------------- selección
def tiene_propuesta(op):
    return "dossier" in op["categorias_propuesta"] or "presupuesto" in op["categorias_propuesta"] \
        or "pliegos" in op["categorias_propuesta"] or op["archivos_fuera_plantilla"] > 0


def grupo(sit):
    for g in ("ganada", "perdida", "dormida", "en curso", "sin negocio"):
        if sit.startswith(g):
            return g
    return "otro"


def cmd_seleccionar(args):
    ops = oportunidades()
    if args.todas:
        prop = [i for i, o in ops.items() if o["archivos_propuesta"] or o["archivos_fuera_plantilla"]]
        proy = [i for i, o in ops.items() if o["situacion"].startswith("ganada") and o["archivos_proyecto"]]
    else:
        proy = []
        for patron in PILOTO_PROYECTOS:
            c = [i for i, o in ops.items() if patron.lower() in o["carpeta"].lower() and o["archivos_proyecto"]]
            if not c:
                print(f"  ¡Ojo! no encuentro el proyecto del piloto «{patron}»")
            proy += c[:1]
        fijas = [i for i, o in ops.items() for p in PILOTO_PROPUESTAS_FIJAS if p.lower() in o["carpeta"].lower()]
        rnd = random.Random(2026)
        candidatas = [i for i, o in ops.items() if tiene_propuesta(o) and o["archivos_propuesta"] > 0]
        prop = list(dict.fromkeys(fijas + proy))
        for g, cupo in PILOTO_CUPOS.items():
            pool = sorted(i for i in candidatas if grupo(ops[i]["situacion"]) == g and i not in prop)
            prop += rnd.sample(pool, min(cupo, len(pool)))
    escribir(SELECCION, {"propuestas": prop, "proyectos": proy, "piloto": not args.todas})
    print(f"Seleccionadas {len(prop)} oportunidades para propuestas y {len(proy)} proyectos:")
    for i in proy:
        print(f"  proyecto · {ops[i]['carpeta'][:70]} ({ops[i]['archivos_proyecto']} archivos)")
    if not args.todas:
        for i in prop:
            print(f"  propuesta · {grupo(ops[i]['situacion']):11s} · {ops[i]['carpeta'][:70]}")


def seleccion(clave):
    s = leer(SELECCION)
    if not s:
        sys.exit("Ejecuta antes 'python fichas.py seleccionar --piloto' (o --todas).")
    return s[clave]


# ---------------------------------------------------------------- fase 2: propuestas
SYSTEM_PROPUESTA = f"""Eres documentalista de alegria-activity, agencia de marketing experiencial que hace campañas, \
roadshows y exposiciones itinerantes en vehículos de flota propia ({", ".join(FLOTA)}) y que también fabrica \
y vende unidades móviles a medida (oficinas bancarias, unidades sanitarias, bibliobuses, puestos de mando…).
Te paso los documentos de la propuesta comercial de una oportunidad (dossier, presupuesto, pliegos) y los datos \
del negocio en el CRM. Rellena la ficha llamando a guardar_ficha_propuesta, sin escribir texto antes.
Reglas:
1. Solo lo que digan los documentos. Si algo no aparece, déjalo vacío o null. No inventes cifras, fechas ni vehículos.
2. Vehículos: sigue esta guía.
{GUIA_FLOTA}
3. Si un documento es de otra oportunidad o está vacío, dilo en notas.
4. No incluyas nombres ni datos de personas (salvo cargos institucionales del cliente).
5. Resumen en castellano, concreto, pensado para que un comercial sepa en 30 segundos qué se presentó."""

HERRAMIENTA_PROPUESTA = {
    "name": "guardar_ficha_propuesta",
    "description": "Guarda la ficha de lo que se presentó al cliente.",
    "input_schema": {"type": "object", "properties": {
        "titulo": {"type": "string", "description": "Nombre de la campaña o del proyecto propuesto"},
        "cliente_final": {"type": "string"},
        "agencia_intermediaria": {"type": "string", "description": "Agencia por la que llega, si la hay"},
        "anio": {"type": ["integer", "null"]},
        "idiomas": {"type": "array", "items": {"type": "string"}},
        "tipo": {"type": "string", "enum": ["campaña o servicio", "venta o fabricación de unidad móvil", "alquiler",
                                            "mixto", "desconocido"]},
        "que_se_propuso": {"type": "string", "description": "3-6 frases"},
        "concepto_creativo": {"type": "string"},
        **CAMPOS_VEHICULO,
        "lineas_servicio": {"type": "array", "items": {"type": "string", "enum": LINEAS}, "maxItems": 3},
        "formato": {"type": "array", "items": {"type": "string"},
                    "description": "p. ej. roadshow, exposición itinerante, escape room, taller, sampling"},
        "tematicas": {"type": "array", "items": {"type": "string"}},
        "publico": {"type": "string"},
        "ambito_geografico": {"type": "string", "description": "Ciudades, regiones o países previstos"},
        "duracion": {"type": "string"},
        "elementos_experiencia": {"type": "array", "items": {"type": "string"},
                                  "description": "pantallas, realidad virtual, photocall, mostrador, aula…"},
        "importe_propuesto": {"type": ["number", "null"], "description": "Total sin IVA si se puede"},
        "moneda": {"type": "string"},
        "desglose_presupuesto": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
            "concepto": {"type": "string"}, "importe": {"type": ["number", "null"]}}}},
        "concurso_publico": {"type": "boolean"},
        "organismo_y_expediente": {"type": "string"},
        "criterios_adjudicacion": {"type": "string"},
        "documentos": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "tipo": {"type": "string"},
            "descripcion": {"type": "string", "description": "Una frase corta"}},
            "required": ["id", "tipo"]}},
        "palabras_clave": {"type": "array", "items": {"type": "string"}, "maxItems": 15},
        "calidad_informacion": {"type": "string", "enum": ["buena", "parcial", "pobre"]},
        "notas": {"type": "string"},
    }, "required": ["titulo", "que_se_propuso", "tipo", "documentos", "calidad_informacion"]},
}

CUPOS_PROPUESTA = {"dossier": (2, 20000), "presupuesto": (1, 8000), "pliegos": (2, 15000), "extra": (1, 15000)}
PREFERENCIA_EXT = {"pdf": 0, "pptx": 1, "docx": 1, "xlsx": 1, "key": 2, "ppt": 2, "doc": 2, "xls": 2}


def elegir_docs_propuesta(archivos):
    """Las versiones más recientes de dossier, presupuesto y pliegos (sin personales ni archivos repetidos)."""
    legibles = [a for a in archivos if a["tipo"] in LEGIBLES and a["sensibilidad"] != "personal"]
    por_cat = defaultdict(list)
    for a in legibles:
        n = inv.normal(a["nombre"])
        if a["parte"] == "propuesta" and a["categoria"] in ("dossier", "presupuesto", "pliegos"):
            por_cat[a["categoria"]].append(a)
        elif a["parte"] == "fuera de plantilla" and re.search(r"dossier|propuesta|ppta|presupuesto|ppto|pliego", n):
            por_cat["presupuesto" if re.search(r"presupuesto|ppto", n) else "dossier"].append(a)
        elif a["parte"] == "propuesta" and a["categoria"] in ("diseños de propuesta", "contenidos") and a["ext"] in ("pdf", "pptx"):
            por_cat["extra"].append(a)
    elegidos, limites = [], {}
    for cat, (n, lim) in CUPOS_PROPUESTA.items():
        if cat == "extra" and por_cat.get("dossier"):
            continue
        vistos = set()
        orden = sorted(por_cat.get(cat, []), key=lambda a: (a["modificado"], -PREFERENCIA_EXT.get(a["ext"], 3)), reverse=True)
        if cat == "pliegos":   # mejor técnico y administrativo que dos versiones del mismo
            orden.sort(key=lambda a: 0 if re.search(r"tecnic|ppt|prescripcion", inv.normal(a["nombre"])) else
                       1 if re.search(r"administ|pcap|clausula", inv.normal(a["nombre"])) else 2)
        for a in orden:
            base = re.sub(r"\b(v\d+|final|def|definitivo|baja|alta|copia|\d+)\b", "", inv.normal(a["nombre"].rsplit(".", 1)[0])).strip()
            if base in vistos:
                continue
            vistos.add(base)
            elegidos.append(dict(a, cat=cat))
            limites[a["id"]] = lim
            if len([e for e in elegidos if e["cat"] == cat]) >= n:
                break
    return elegidos, limites


def cmd_propuestas(args):
    ops = oportunidades()
    hechas = resultados("propuestas") if not args.forzar else {}
    ids = [i for i in seleccion("propuestas") if i not in hechas]
    print(f"Preparando {len(ids)} propuestas (leyendo texto de Box)…")
    peticiones, sin_docs = [], 0
    for k, cid in enumerate(ids, 1):
        op = ops[cid]
        docs, limites = elegir_docs_propuesta(archivos_de(cid))
        textos = leer_textos(docs, limites)
        bloques = [f"[{d['cat'].upper()}] {d['nombre']} (id {d['id']}, ruta {d['ruta']}, modificado {d['modificado']})\n"
                   f"{textos[d['id']] or '(Box no ha podido extraer texto de este archivo)'}" for d in docs]
        if not any(textos.values()):
            sin_docs += 1
            continue
        contenido = contexto_negocio(op) + "\n\n" + "\n\n---\n\n".join(bloques)
        peticiones.append(peticion(f"p_{cid}", SYSTEM_PROPUESTA, contenido, HERRAMIENTA_PROPUESTA, 6000))
        if k % 25 == 0:
            print(f"  {k}/{len(ids)}")
    if sin_docs:
        print(f"  {sin_docs} oportunidades sin texto legible (se saltan)")
    if enviar("propuestas", peticiones, 900, args):
        esperar_y_recoger("propuestas")


# ---------------------------------------------------------------- fase 3a: mapa de carpetas
SYSTEM_MAPA = f"""Eres documentalista de alegria-activity (agencia de marketing experiencial con vehículos de flota \
propia: {", ".join(FLOTA)}). Te paso el árbol de carpetas de un proyecto YA VENDIDO, con el número de archivos de \
cada carpeta por tipo y una muestra de nombres con su id. No ves el contenido, solo los nombres.
Llama a guardar_mapa_proyecto, sin escribir texto antes. Rellena PRIMERO documentos_clave.
1. secciones (breves; como mucho 15): agrupa las carpetas por lo que contienen, usando las categorías permitidas. Una carpeta con miles de \
archivos parecidos (inscripciones, hojas de firmas, certificados por colegio, fotos de público) se describe en una \
línea; no es más importante por tener muchos archivos.
2. documentos_clave: hasta 15 ids (SOLO de los que aparecen en el árbol) que mejor cuenten el proyecto: memoria o \
informe final, resumen de asistencia o resultados, hoja de ruta o calendario, ficha técnica del vehículo, permisos \
tipo, manual o guion de la experiencia, contrato principal. Si hay informes periódicos (mensuales, semanales), elige \
el ÚLTIMO y el final o memoria, que acumulan los datos, y cualquier hoja de datos de visitantes o asistencia. Evita \
duplicados y versiones antiguas.
3. No incluyas nombres de personas."""

HERRAMIENTA_MAPA = {
    "name": "guardar_mapa_proyecto",
    "description": "Guarda el mapa de la documentación del proyecto.",
    "input_schema": {"type": "object", "properties": {
        "documentos_clave": {"type": "array", "maxItems": 15, "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "motivo": {"type": "string"}}, "required": ["id", "motivo"]}},
        "resumen_estructura": {"type": "string", "description": "2-3 frases: cómo está organizada la documentación"},
        "secciones": {"type": "array", "items": {"type": "object", "properties": {
            "categoria": {"type": "string", "enum": SECCIONES},
            "rutas": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "descripcion": {"type": "string"},
            "archivos": {"type": "integer"}},
            "required": ["categoria", "rutas", "descripcion"]}},
    }, "required": ["documentos_clave", "resumen_estructura", "secciones"]},
}


def arbol(archivos, max_chars=80000):
    """Árbol de carpetas compacto: recuentos por tipo y muestra de nombres (prioriza documentos legibles)."""
    por_dir = defaultdict(list)
    personales = 0
    for a in archivos:
        if a["sensibilidad"] == "personal":
            personales += 1
            continue
        por_dir[a["ruta"]].append(a)
    for muestra in (12, 6, 3, 1):
        lineas = []
        for ruta in sorted(por_dir):
            fs = por_dir[ruta]
            tipos = Counter(a["tipo"] for a in fs)
            lineas.append(f"📁 {ruta or '(raíz)'} — {len(fs)} archivos: " + ", ".join(f"{v} {k}" for k, v in tipos.most_common()))
            docs = sorted([a for a in fs if a["tipo"] in LEGIBLES], key=lambda a: a["modificado"], reverse=True)
            otros = [a for a in fs if a["tipo"] not in LEGIBLES]
            for a in docs[:muestra]:
                lineas.append(f"   - {a['nombre']} [id {a['id']}] ({a['modificado']})")
            if len(docs) > muestra:
                lineas.append(f"   … y {len(docs) - muestra} documentos más")
            if otros and not docs:
                lineas += [f"   - {a['nombre']}" for a in otros[:2]]
        texto = "\n".join(lineas)
        if len(texto) <= max_chars:
            break
    if len(texto) > max_chars:
        texto = texto[:max_chars] + "\n[árbol recortado]"
    if personales:
        texto += f"\n({personales} archivos de datos personales no listados)"
    return texto


def cmd_mapas(args):
    ops = oportunidades()
    hechas = resultados("mapas") if not args.forzar else {}
    peticiones = []
    for cid in seleccion("proyectos"):
        if cid in hechas:
            continue
        op = ops[cid]
        proyecto = [a for a in archivos_de(cid) if a["parte"] != "propuesta"]
        contenido = contexto_negocio(op) + f"\n\nÁRBOL DEL PROYECTO ({len(proyecto)} archivos)\n" + arbol(proyecto)
        peticiones.append(peticion(f"m_{cid}", SYSTEM_MAPA, contenido, HERRAMIENTA_MAPA, 8000))
    if enviar("mapas", peticiones, 1500, args):
        esperar_y_recoger("mapas")


# ---------------------------------------------------------------- fase 3b: ficha del proyecto
SYSTEM_PROYECTO = f"""Eres documentalista de alegria-activity (agencia de marketing experiencial con vehículos de \
flota propia: {", ".join(FLOTA)}; también fabrica unidades móviles para clientes). Te paso los datos de un proyecto \
YA VENDIDO, el mapa de su documentación y el texto de sus documentos clave. Llama a guardar_ficha_proyecto, sin \
escribir texto antes.
Reglas:
1. Solo lo que digan los documentos; si no aparece, vacío o null. Las cifras (visitantes, paradas, fechas) tal cual \
aparezcan, indicando de qué documento salen.
2. Cuenta lo REALIZADO (informes, memorias, rutas definitivas, permisos concedidos), no lo propuesto: no repitas la \
propuesta. Si hay informes periódicos, usa el último o el final, que acumulan los datos.
3. Rellena siempre visitantes_detalle: la cifra de visitantes, participantes, grupos o sesiones y de qué documento \
sale; si no aparece en ningún documento, escribe "no consta en los documentos leídos".
4. Vehículos: sigue esta guía.
{GUIA_FLOTA}
5. Sin nombres ni datos de personas (solo cargos institucionales del cliente).
6. En castellano, concreto, pensado para que alguien de la agencia encuentre después qué se hizo y dónde está."""

HERRAMIENTA_PROYECTO = {
    "name": "guardar_ficha_proyecto",
    "description": "Guarda la ficha del proyecto realizado.",
    "input_schema": {"type": "object", "properties": {
        "resumen": {"type": "string", "description": "Qué se hizo realmente, 4-8 frases"},
        "fechas": {"type": "string"},
        **CAMPOS_VEHICULO,
        "recorrido": {"type": "array", "items": {"type": "string"}, "description": "Ciudades, sedes o países visitados"},
        "paradas": {"type": ["integer", "null"]},
        "visitantes_total": {"type": ["integer", "null"], "description": "Personas atendidas o visitantes, si consta"},
        "visitantes_detalle": {"type": "string", "description": "Cifras (visitantes, grupos, sesiones, encuestas) y documento del que salen"},
        "visitantes_documento_id": {"type": "string"},
        "resultados": {"type": "array", "items": {"type": "string"}, "description": "KPIs y resultados con su cifra"},
        "experiencia": {"type": "string", "description": "Cómo era la experiencia o el espacio"},
        "permisos_y_tramites": {"type": "string"},
        "proveedores_y_socios": {"type": "array", "items": {"type": "string"}},
        "incidencias_y_aprendizajes": {"type": "string"},
        "documentos": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "tipo": {"type": "string"}, "descripcion": {"type": "string"}},
            "required": ["id", "tipo", "descripcion"]}},
        "palabras_clave": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "calidad_informacion": {"type": "string", "enum": ["buena", "parcial", "pobre"]},
    }, "required": ["resumen", "visitantes_detalle", "documentos", "calidad_informacion"]},
}


PATRON_CLAVE = re.compile(r"informe|memoria|datos|participaci|asistencia|visitante|registro|balance|resultado|"
                          r"ruta|calendario|kpi|estadistic|resumen|cierre|final")


def completar_clave(clave, archivos, maximo=15):
    """Si el mapa se queda corto, añade por nombre informes, memorias, datos de participación, registros y rutas."""
    ya = {a["id"] for a in clave}
    candidatos = [a for a in archivos if a["id"] not in ya and a["tipo"] in LEGIBLES and a["sensibilidad"] != "personal"
                  and a["parte"] != "propuesta"
                  and PATRON_CLAVE.search(inv.normal(a["nombre"] + " " + a["ruta"].split("/")[-1]))]
    candidatos.sort(key=lambda a: (bool(PATRON_CLAVE.search(inv.normal(a["nombre"]))),
                                   a["ext"] in ("xlsx", "xls", "pdf"), a["modificado"]), reverse=True)
    vistos = set()
    for a in candidatos:
        if len(clave) >= maximo:
            break
        base = re.sub(rf"\b({MESES})\b", "", re.sub(r"\d+", "", inv.normal(a["nombre"])))
        base = re.sub(r"\s+", " ", base).strip()[:40]
        if base in vistos:          # informes mensuales con el mismo nombre: solo el más reciente
            continue
        vistos.add(base)
        clave.append(a)
    return clave


def cmd_proyectos(args):
    ops = oportunidades()
    mapas = resultados("mapas")
    hechas = resultados("proyectos") if not args.forzar else {}
    peticiones = []
    for cid in seleccion("proyectos"):
        if cid in hechas:
            continue
        mapa = mapas.get(cid)
        if not mapa:
            print(f"  sin mapa todavía: {ops[cid]['carpeta'][:60]} (ejecuta antes 'mapas')")
            continue
        por_id = {a["id"]: a for a in archivos_de(cid)}
        propuestos = mapa.get("documentos_clave", [])
        clave = [por_id[d["id"]] for d in propuestos
                 if d.get("id") in por_id and por_id[d["id"]]["tipo"] in LEGIBLES
                 and por_id[d["id"]]["sensibilidad"] != "personal"][:15]
        validos = len(clave)
        clave = completar_clave(clave, list(por_id.values()), maximo=max(10, validos))
        textos = leer_textos(clave, {a["id"]: 12000 for a in clave})
        print(f"  {ops[cid]['carpeta'][:50]}: el mapa propone {len(propuestos)} documentos, {validos} válidos, "
              f"+{len(clave) - validos} añadidos por nombre, {sum(1 for t in textos.values() if t)} con texto", flush=True)
        motivo = {d["id"]: d.get("motivo", "") for d in propuestos}
        bloques = [f"[{a['nombre']}] (id {a['id']}, ruta {a['ruta']}, motivo: {motivo.get(a['id'], '')})\n"
                   f"{textos[a['id']] or '(sin texto extraíble)'}" for a in clave]
        prop = resultados("propuestas").get(cid)
        contenido = (contexto_negocio(ops[cid])
                     + (f"\n\nTítulo de la propuesta: {prop.get('titulo', '')} (lo propuesto no interesa aquí)" if prop else "")
                     + "\n\nMAPA DE LA DOCUMENTACIÓN\n" + json.dumps(mapa, ensure_ascii=False)
                     + "\n\nDOCUMENTOS CLAVE\n" + "\n\n---\n\n".join(bloques))
        peticiones.append(peticion(f"y_{cid}", SYSTEM_PROYECTO, contenido, HERRAMIENTA_PROYECTO, 8000))
    if enviar("proyectos", peticiones, 1500, args):
        esperar_y_recoger("proyectos")


# ---------------------------------------------------------------- índice y revisión
def _lista(x):
    """Lista de textos aunque el modelo devuelva un texto suelto, None o elementos raros."""
    if x is None:
        return []
    if isinstance(x, str):
        return [x] if x.strip() else []
    if isinstance(x, list):
        return [e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) for e in x if e]
    return [str(x)]


def _secciones(m):
    """Secciones del mapa como diccionarios, aunque alguna venga como texto."""
    out = []
    for sec in _lista_cruda((m or {}).get("secciones")):
        if isinstance(sec, dict):
            out.append({"categoria": str(sec.get("categoria", "otros")), "rutas": _lista(sec.get("rutas")),
                        "descripcion": str(sec.get("descripcion", "")), "archivos": sec.get("archivos")})
        elif isinstance(sec, str) and sec.strip():
            out.append({"categoria": "otros", "rutas": [], "descripcion": sec, "archivos": None})
    return out


def _lista_cruda(x):
    if x is None:
        return []
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else [x]
        except ValueError:
            return [x]
    return x if isinstance(x, list) else [x]


def cmd_indice(_args):
    ops = oportunidades()
    props, mapas, proys = resultados("propuestas"), resultados("mapas"), resultados("proyectos")
    salida, filas = [], []
    for cid in sorted(set(props) | set(mapas) | set(proys)):
        op = ops[cid]
        archivos = archivos_de(cid)
        fotos = sum(1 for a in archivos if a["tipo"] == "imagen" and a["parte"] == "proyecto")
        p, m, y = props.get(cid), mapas.get(cid), proys.get(cid)
        if m:
            m = dict(m, secciones=_secciones(m),
                     documentos_clave=[d for d in _lista_cruda(m.get("documentos_clave")) if isinstance(d, dict)])
        es_venta = (p or {}).get("tipo") == "venta o fabricación de unidad móvil"
        for f in (p, y):
            if f:
                f["vehiculo"] = normalizar_vehiculo(f.get("vehiculo"), f.get("vehiculo_descripcion"), es_venta)
        reg = {
            "carpeta_id": cid, "carpeta": op["carpeta"], "pais": op["pais"], "enlace_box": op["enlace_box"],
            "situacion": op["situacion"], "negocio_id": op["negocio_id"], "etapa": op["etapa"], "anio": op["anio"],
            "fecha_cierre": op["fecha_cierre"], "cliente": op["cliente"], "comercial": op["comercial"],
            "carpetas_gemelas": op["carpetas_gemelas"], "fotos_en_box": fotos,
            # Datos económicos aparte, para poder ocultarlos a usuarios con acceso limitado
            "economico": {"importe_negocio": op["importe"],
                          "importe_propuesto": (p or {}).get("importe_propuesto"),
                          "desglose_presupuesto": (p or {}).get("desglose_presupuesto")},
            "propuesta": {k: v for k, v in (p or {}).items() if k not in ("importe_propuesto", "desglose_presupuesto")} or None,
            "mapa": m, "proyecto": y,
        }
        salida.append(reg)
        filas.append({
            "carpeta": op["carpeta"], "situacion": op["situacion"], "anio": op["anio"], "enlace_box": op["enlace_box"],
            "propuesta_titulo": (p or {}).get("titulo", ""), "propuesta_resumen": (p or {}).get("que_se_propuso", ""),
            "propuesta_vehiculo": str((p or {}).get("vehiculo", "") or ""),
            "propuesta_vehiculo_descripcion": (p or {}).get("vehiculo_descripcion", ""),
            "propuesta_importe": (p or {}).get("importe_propuesto", ""),
            "propuesta_calidad": (p or {}).get("calidad_informacion", ""),
            "proyecto_resumen": (y or {}).get("resumen", ""),
            "proyecto_recorrido": ", ".join(_lista((y or {}).get("recorrido"))),
            "proyecto_visitantes": (y or {}).get("visitantes_total", ""),
            "proyecto_visitantes_detalle": (y or {}).get("visitantes_detalle", ""),
            "proyecto_vehiculo": (y or {}).get("vehiculo", ""),
            "proyecto_vehiculo_descripcion": (y or {}).get("vehiculo_descripcion", ""),
            "mapa_secciones": "; ".join(f"{s['categoria']}: {', '.join(s['rutas'][:3])}" for s in (m or {}).get("secciones", [])),
            "revision_ok": "", "notas_revision": "",
        })
    inv.escribir_json(INDICE, {"generado": inv.ahora(), "version": VERSION, "oportunidades": salida})
    inv._csv(REVISION, filas)
    print(f"Índice: {len(salida)} oportunidades ({len(props)} propuestas, {len(mapas)} mapas, {len(proys)} proyectos) "
          f"→ {INDICE}\nRevisión → {REVISION}")


# ---------------------------------------------------------------- actualización incremental
def _hash(partes):
    import hashlib
    return hashlib.sha1("\n".join(sorted(partes)).encode()).hexdigest()[:16]


def huella_propuesta(archivos):
    """Cambia si cambian los documentos que se leerían para la ficha de propuesta."""
    docs, _ = elegir_docs_propuesta(archivos)
    return _hash(f"{d['id']}:{d['modificado']}" for d in docs) if docs else None


def huella_proyecto(archivos):
    """Cambia si se añaden, quitan o modifican documentos del proyecto (no fotos ni archivos personales)."""
    docs = [a for a in archivos if a["parte"] != "propuesta" and a["tipo"] in LEGIBLES and a["sensibilidad"] != "personal"]
    return _hash(f"{a['id']}:{a['modificado']}" for a in docs) if docs else None


def cmd_actualizar(args):
    import fcntl
    CERROJO.parent.mkdir(parents=True, exist_ok=True)
    cerrojo = CERROJO.open("w")
    try:
        fcntl.flock(cerrojo, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Ya hay una actualización en marcha.")
    t0 = time.time()
    print(f"=== Actualización {inv.ahora()} UTC ===", flush=True)
    a = argparse.Namespace(forzar=False, si=True, todas=False)
    inv.cmd_hubspot(a)
    inv.cmd_box(a)
    inv.cmd_cruzar(a)

    ops = oportunidades()
    huellas = leer(HUELLAS, None)
    props, mapas, proys = resultados("propuestas"), resultados("mapas"), resultados("proyectos")
    primera = huellas is None
    huellas = huellas or {}
    rehacer_p, rehacer_y = [], []
    nuevas = {}
    for cid, op in ops.items():
        tiene_prop = op["archivos_propuesta"] or op["archivos_fuera_plantilla"]
        es_proyecto = op["situacion"].startswith("ganada") and op["archivos_proyecto"]
        if not tiene_prop and not es_proyecto:
            continue
        archivos = archivos_de(cid)
        h = {"p": huella_propuesta(archivos) if tiene_prop else None,
             "y": huella_proyecto(archivos) if es_proyecto else None}
        nuevas[cid] = h
        previa = huellas.get(cid)
        if primera and previa is None:
            # Primera vez: lo que ya tiene ficha se da por bueno; solo se hace lo que falta
            if h["p"] and cid not in props:
                rehacer_p.append(cid)
            if h["y"] and (cid not in mapas or cid not in proys):
                rehacer_y.append(cid)
            continue
        if h["p"] and (cid not in props or (previa or {}).get("p") != h["p"]):
            rehacer_p.append(cid)
        if h["y"] and (cid not in proys or (previa or {}).get("y") != h["y"]):
            rehacer_y.append(cid)

    total = len(rehacer_p) + 2 * len(rehacer_y)
    print(f"Para rehacer: {len(rehacer_p)} propuestas y {len(rehacer_y)} proyectos"
          + (" (primera pasada: se guardan las huellas de lo ya hecho)" if primera else ""), flush=True)
    if total > MAX_NOCTURNO and not getattr(args, "si", False):
        print(f"¡Demasiado para una actualización automática ({total} peticiones > {MAX_NOCTURNO})! "
              f"No se envía nada. Revísalo y lánzalo a mano con: python fichas.py actualizar --si", flush=True)
        rehacer_p, rehacer_y = [], []
    hechas_p, hechas_y = set(), set()
    if rehacer_p or rehacer_y:
        escribir(SELECCION, {"propuestas": rehacer_p, "proyectos": rehacer_y, "piloto": False, "actualizacion": inv.ahora()})
        b = argparse.Namespace(si=True, forzar=True)
        if rehacer_p:
            cmd_propuestas(b)
            hechas_p = set(rehacer_p) & set(resultados("propuestas"))
        if rehacer_y:
            cmd_mapas(b)
            cmd_proyectos(b)
            hechas_y = set(rehacer_y) & set(resultados("proyectos"))
    # Guardar huellas: lo que ya estaba bien + lo que se ha hecho ahora (lo fallido se reintentará mañana)
    for cid, h in nuevas.items():
        previa = huellas.get(cid, {})
        huellas[cid] = {
            "p": h["p"] if (cid in hechas_p or (cid not in rehacer_p and h["p"])) else previa.get("p"),
            "y": h["y"] if (cid in hechas_y or (cid not in rehacer_y and h["y"])) else previa.get("y"),
        }
    escribir(HUELLAS, huellas)
    cmd_indice(args)
    print(f"=== Fin {inv.ahora()} UTC · {int(time.time() - t0) // 60} min ===", flush=True)


# ---------------------------------------------------------------- main
def cmd_estado(_args):
    lotes = leer(LOTES, {}) or {}
    if not lotes:
        sys.exit("No hay lotes enviados.")
    cli = cliente_api()
    for fase, v in lotes.items():
        for lid in v["ids"]:
            b = cli.messages.batches.retrieve(lid)
            c = b.request_counts
            print(f"  {fase:10s} {lid} {b.processing_status:12s} ok={c.succeeded} error={c.errored} "
                  f"en curso={c.processing} · recogido={v.get('recogido')} · coste={v.get('coste_usd', '?')} $")


def main():
    ap = argparse.ArgumentParser(description="Fichas de oportunidades con Haiku")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seleccionar")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--piloto", action="store_true")
    g.add_argument("--todas", action="store_true")
    for nombre in ("propuestas", "mapas", "proyectos", "piloto"):
        p = sub.add_parser(nombre)
        p.add_argument("--si", action="store_true", help="no pedir confirmación")
        p.add_argument("--forzar", action="store_true", help="rehacer aunque ya haya ficha")
    sub.add_parser("estado")
    sg = sub.add_parser("seguir")
    sg.add_argument("fase", choices=["propuestas", "mapas", "proyectos"])
    sub.add_parser("indice")
    ac = sub.add_parser("actualizar")
    ac.add_argument("--si", action="store_true", help="no aplicar el tope de fichas por noche")
    args = ap.parse_args()
    F.mkdir(parents=True, exist_ok=True)
    if args.cmd == "seleccionar":
        cmd_seleccionar(args)
    elif args.cmd == "propuestas":
        cmd_propuestas(args)
    elif args.cmd == "mapas":
        cmd_mapas(args)
    elif args.cmd == "proyectos":
        cmd_proyectos(args)
    elif args.cmd == "piloto":
        if not SELECCION.exists():
            args.todas = False
            cmd_seleccionar(args)
        cmd_propuestas(args)
        cmd_mapas(args)
        cmd_proyectos(args)
        cmd_indice(args)
    elif args.cmd == "estado":
        cmd_estado(args)
    elif args.cmd == "seguir":
        esperar_y_recoger(args.fase)
    elif args.cmd == "indice":
        cmd_indice(args)
    elif args.cmd == "actualizar":
        cmd_actualizar(args)


if __name__ == "__main__":
    main()

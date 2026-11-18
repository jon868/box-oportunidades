# Índice de oportunidades (Box + HubSpot)

Base de la skill **buscar oportunidades**: qué le presentamos a cada cliente, qué se vendió y dónde está
toda la documentación del proyecto (fotos, vehículo, rutas, asistencia, planos…).

- **HubSpot** dice qué pasó con cada negocio: etapa (ganada, perdida, dormida, en curso), importe, fecha,
  cliente y comercial. Incluye el histórico importado de Salesforce.
- **Box** (`0.Salesforce / Oportunidades`, id 3046666597) dice qué documentación se guardó.
  Cada oportunidad tiene `Propuesta Comercial` y `Proyecto` con subcarpetas de plantilla.
- El cruce se hace por la propiedad `box_opp_folderid` del negocio, por el ID de HubSpot al principio del
  nombre de la carpeta (carpetas nuevas) o, si no hay otra cosa, por nombre exacto.

**Carpetas de dos épocas.** Hasta 2026 las carpetas las creaba Salesforce; ahora las crea HubSpot al dar de alta
el negocio, con la misma plantilla y el ID del negocio delante del nombre. Todo el histórico de Salesforce se importó
a HubSpot, así que una misma oportunidad puede tener **dos carpetas gemelas** (`ABAI GROUP_X` y
`519264768186 - ABAI GROUP_X`). El script las agrupa bajo el mismo negocio (columna `carpetas_gemelas`) y no da por
hueco un negocio cuya documentación está en la gemela.

**Una carpeta vacía no significa que no pasara nada.** Si el negocio está ganado y no hay documentación,
sale en `huecos.csv` para que alguien la complete.

Solo **lee** de Box y de HubSpot: no escribe, mueve ni borra nada.

## Fases

| Fase | Qué hace | Coste de API de Claude |
|---|---|---|
| 1. Inventario (este script) | Cruza carpetas y negocios, cuenta archivos por parte, categoría, tipo y sensibilidad | 0 |
| 2. Fichas de propuesta | Lee dossier, presupuesto y pliegos de cada oportunidad presentada → qué se presentó | se calcula con el inventario |
| 3. Fichas de proyecto | Lee y cataloga los documentos de las ganadas; enlaza las fotos con el índice de fotos ya existente | se calcula con el inventario |
| 4. Buscador | `buscar_oportunidades` en el conector box-mcp + skill para Claude Team y Open WebUI | 0 |

## 1. Antes de empezar
- **HubSpot** → Development → Keys → Service keys → **Create service key**, nombre `inventario-oportunidades`,
  con estos permisos **solo de lectura**: `crm.objects.deals.read`, `crm.objects.companies.read`,
  `crm.objects.owners.read`. Crear → abrir la clave → Show → Copy. (Las apps privadas antiguas ya no se pueden crear;
  la clave de servicio se usa igual, como `Bearer`.) Hace falta ser superadministrador o tener acceso a
  herramientas de desarrollador.
- Ten a mano las credenciales de la app de Box "claudebox" (las mismas del conector y del etiquetado).

## 2. Crear el servicio en Easypanel
1. **+ Service → App**, nombre `oportunidades`, en el proyecto `tools`.
2. **Source → GitHub**: este repositorio, rama `main`. **Build**: Dockerfile.
3. **Environment** (los valores, nunca en el repo):
   ```
   BOX_CLIENT_ID=...
   BOX_CLIENT_SECRET=...
   BOX_SUBJECT_TYPE=enterprise
   BOX_SUBJECT_ID=...
   BOX_FOLDER_ID=3046666597
   HUBSPOT_TOKEN=...
   DESCARGA_USUARIO=alegria
   DESCARGA_PASSWORD=una-contraseña-larga
   ```
4. **Mounts**: volumen `datos` en `/app/datos`.
5. **Domains**: dominio HTTPS al puerto `8080`.
6. **Deploy**.

## 3. Ejecutar el inventario (consola del servicio)
```bash
python inventario.py hubspot      # ~1 min: unos 5.200 negocios
setsid nohup python inventario.py box > /app/datos/log.txt 2>&1 < /dev/null &   # en segundo plano
tail -f /app/datos/log.txt        # Ctrl+C deja de mirar; el proceso sigue
python inventario.py cruzar       # cuando termine 'box': genera los informes
```
- `box` no entra en carpetas que Box dice que pesan 0, guarda la lista de archivos de cada oportunidad en
  `datos/carpetas/<id>.json` (para no tenerlo todo en memoria), guarda el avance cada 100 y reanuda si se corta
  (relanza la misma línea). En pasadas posteriores solo vuelve a leer las carpetas que han cambiado.
- No redespliegues el servicio mientras corre.

## 4. Resultados (`https://<dominio>/`, con el usuario y la contraseña de descarga)
- `resumen.txt`: cuántas carpetas hay en cada situación, cómo se han cruzado, archivos por tipo y por sensibilidad,
  y cuántos documentos habría que leer en las fases 2 y 3.
- `inventario.csv`: una fila por carpeta (situación, negocio, etapa, importe, cliente, comercial, archivos por parte
  y categoría, enlace a Box). Se abre en Excel con `;` como separador.
- `huecos.csv`: ganadas sin documentación o solo con propuesta, de mayor a menor importe.
- `negocios_sin_carpeta.csv`: negocios de HubSpot sin carpeta en Oportunidades (sin carpeta, carpeta en otro sitio
  de Box o carpeta borrada).

Situaciones posibles: `ganada documentada`, `ganada solo con propuesta`, `ganada SIN documentación`,
`perdida/dormida con propuesta`, `perdida/dormida sin documentación`, `en curso con/sin documentación`,
`sin negocio · con proyecto/con propuesta/vacía`.

## 5. Fichas con Haiku (fases 2 y 3) · `fichas.py`
Añade al servicio `ANTHROPIC_API_KEY` (una clave propia, p. ej. `oportunidades`, con límite de gasto) y redespliega.
Usa la Batch API desde el servidor: **no gasta nada de Claude Team**.
```bash
python fichas.py seleccionar --piloto   # 5 proyectos ganados + ~36 propuestas variadas (fijo, reproducible)
setsid nohup python fichas.py piloto --si > /app/datos/fichas/log.txt 2>&1 < /dev/null &
tail -f /app/datos/fichas/log.txt
```
`piloto` hace, esperando a que acabe cada lote:
1. **propuestas**: última versión de dossier, presupuesto y pliegos → ficha "qué se presentó".
2. **mapas**: solo el árbol de carpetas y nombres de cada proyecto ganado → secciones y hasta 15 documentos clave.
   Una carpeta con miles de inscripciones o fotos queda descrita en una línea, sin leerla.
3. **proyectos**: lee los documentos clave → ficha del proyecto (recorrido, asistencia, resultados, permisos…).
4. **indice**: `fichas/indice_oportunidades.json` (para el buscador) y `fichas/revision.csv` (para revisar a mano,
   con columnas `revision_ok` y `notas_revision`), descargables en `https://<dominio>/fichas/…`.

Sin `--si` enseña el coste estimado y pide confirmación en cada fase. Si se corta: `python fichas.py estado` y
`python fichas.py seguir <fase>`; relanzar una fase solo envía lo que falta (`--forzar` para rehacer).
El texto que extrae Box se guarda en `fichas/textos/` y no se vuelve a pedir.
Los archivos de datos personales nunca se envían a la IA (ni su nombre).
Para todo el archivo, cuando el piloto esté revisado: `python fichas.py seleccionar --todas` y las mismas fases.

## Sensibilidad y permisos
Cada archivo lleva una sensibilidad:
- **general**: dossier, diseños, fotos, planos, secretaría técnica (SecTec: permisos, rutas, convocatorias), otra documentación…
- **economico**: `Presupuestos`, `Contrato` y cualquier archivo cuyo nombre diga presupuesto, factura, contrato,
  tarifa, coste, pedido…
- **personal**: `Personal` y cualquier archivo cuyo nombre diga nómina, DNI, NIE, seguridad social, TC2, CV…
  El contenido de estos archivos **no se lee nunca** en las fases 2 y 3: solo se inventaría que existen.

Hoy todos los usuarios ven todo. Para tener usuarios **completos** y **limitados** (fase 4):
- El conector tendrá dos secretos: `MCP_SECRET` (completo, como ahora) y `MCP_SECRET_LIMITADO`.
  Con el limitado, `buscar_oportunidades` no devuelve importes ni archivos económicos o personales, y
  `box_read_file`, `box_get_download_link`, `box_list_folder` y `box_search` se niegan a abrir esos archivos y
  carpetas. Si no se cierran también esas herramientas, el filtro del buscador sería decorativo.
- Claude Team: el conector de la organización usa la URL limitada; quien tenga acceso completo añade además
  un conector personal con la URL completa.
- Open WebUI: dos conexiones de herramientas, cada una asignada a su grupo de usuarios.

## Probar en local
`pip install -r requirements.txt`, copia `.env.example` a `.env`, rellénalo y ejecuta los mismos comandos.

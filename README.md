# testimonios (FastAPI · Cloud Run · Service Account)

Servicio REST para generar **testimonios**/cartas a partir de transcripciones de llamadas.
Opera con **Service Account (SA)** usando **ADC/Workload Identity** en Cloud Run (sin llaves) y SA JSON opcional en local.

---

## Tabla de contenidos

* [Arquitectura lógica](#arquitectura-lógica)
* [Estructura del proyecto](#estructura-del-proyecto)
* [Endpoints](#endpoints)
* [Esquemas (request/response)](#esquemas-requestresponse)
* [Configuración](#configuración)
* [Ejecución local](#ejecución-local)
* [Despliegue en GCP (Artifact Registry + Cloud Run)](#despliegue-en-gcp-artifact-registry--cloud-run)
* [Pruebas rápidas](#pruebas-rápidas)
* [Permisos, APIs y SA](#permisos-apis-y-sa)
* [Logging](#logging)
* [Manejo de errores](#manejo-de-errores)
* [Solución de problemas](#solución-de-problemas)
* [Notas y buenas prácticas](#notas-y-buenas-prácticas)
* [Tecnologías principales](#tecnologías-principales)
* [Licencia](#licencia)

---

## Arquitectura lógica

* **Autenticación Google**: Service Account (SA).

  * **Cloud Run**: ADC vía identidad del servicio (sin subir llaves).
  * **Local**: opcional `GOOGLE_APPLICATION_CREDENTIALS` apuntando a un JSON de SA.
* **Docs/Drive (Smart Reader)**: Soporta tanto Google Docs nativos como archivos de Microsoft Word (** .docx **). Utiliza un patrón de diseño **Strategy** para extraer texto de forma óptima según el formato.
* **Creación Automática via ForgeDocs**: Si `output_doc_id` no se provee, el servicio llama automáticamente al microservicio **ForgeDocs** para crear uno nuevo en la carpeta configurada y asigna permisos de edición a la SA.
* **Vertex AI**: modelos Gemini vía `vertexai`, con rol `aiplatform.user`.
* **Plantillas**: se usan desde `src/domain/prompts`. Si no se provee una, hay *fallback prompt*.
* **Encadenamiento automático**: soporta webhooks para integrarse con otros servicios (ej: Transcriptor).
* **Callback a Sheets**: puede actualizar Google Sheets al completar el proceso, escribiendo el link del documento generado y el estado.

---

## Estructura del proyecto

```
testimonios/
├── src/
│   ├── main.py                    # Aplicación FastAPI principal
│   ├── settings.py                # Configuración y variables de entorno
│   ├── logging_conf.py            # Configuración de logging
│   ├── auth.py                    # Autenticación con Google (SA)
│   ├── api/
│   │   ├── health.py              # Endpoints de health check
│   │   ├── testimonios.py         # Endpoints de generación de testimonios
│   │   └── middleware/
│   │       └── error_handler.py   # Manejo global de errores
│   ├── domain/
│   │   ├── schemas.py             # Modelos Pydantic (Request/Response)
│   │   ├── prompt_loader.py       # Carga de plantillas de prompts
│   │   └── prompts/               # Plantillas por idioma (es/, en/)
│   ├── orchestration/
│   │   └── runner.py              # Lógica principal de generación
│   └── clients/
│       ├── vertex_client.py       # Cliente Vertex AI (Gemini)
│       ├── gdocs_client.py        # Cliente Google Docs
│       ├── drive_client.py        # Cliente Google Drive
│       ├── sheets_client.py       # Cliente Google Sheets
│       └── gcs_client.py          # Cliente Google Cloud Storage
├── requirements.txt               # Dependencias Python
├── Dockerfile                     # Imagen Docker para Cloud Run
├── .env                           # Variables de entorno (local)
└── README.md                      # Este archivo
```

---

## Endpoints

### `GET /health`

Health básico (sin tocar Google APIs).

#### Ejemplo respuesta

```json
{ "ok": true, "service": "testimonios", "project": "ortega-473114", "region": "us-central1" }
```

### `GET /health/sa?doc_id=...`

Prueba completa: credenciales + Google Docs/Drive + Vertex.
Detecta automáticamente el tipo de archivo (`doc` o `docx`) y realiza la prueba pertinente:
* **Google Doc**: Prueba de escritura reversible (insert + delete).
* **Archivo .docx**: Prueba de lectura de texto (download + extract).

Si no pasas `doc_id`, usa `HEALTHCHECK_DOC_ID` del entorno.

#### Respuesta OK

```json
{ 
  "status": "ok", 
  "doc_id": "1xxxxx", 
  "strategy": "GoogleDocStrategy|WordDocStrategy",
  "operation_test": "write_reversible_ok|read_ok", 
  "vertex": "ok" 
}
```

#### Errores comunes

* 403: Doc no compartido con la SA.
* 404: Doc ID inexistente.
* 400/422: falta `doc_id` y no hay `HEALTHCHECK_DOC_ID`.

### `POST /generate-testimony`

Genera el testimonio y lo **escribe** en un **Google Doc existente**.

Reglas de precedencia de fuente:

1. `raw_text` (usa tal cual, sin inventar),
2. `transcription_doc_id` (**Soporta .doc y .docx**),
3. `transcription_link` → se extrae `doc_id`.

Destino de escritura:

* `output_doc_id`: ID del Google Doc de destino (**Nativo**). 
* **Opcional**: Si se omite, el servicio invocará a `ForgeDocs` para crear uno nuevo automáticamente.

### `POST /webhook/chain`

Endpoint diseñado para **encadenamiento automático** de servicios. Recibe el payload del servicio de Transcripción al terminar de procesar un audio.

* Acepta el mismo esquema `TestimonyRequest`.
* Procesa automáticamente la transcripción completada.
* Soporta callback a Google Sheets para actualizar estado.

---

## Esquemas (request/response)

### Request — `TestimonyRequest`

```json
{
  "case_id": "CASE-001",
  "client": "Nombre Cliente (opcional)",
  "witness": "Nombre Testigo (opcional)",
  "transcription_link": "https://docs.google.com/document/d/1ABC.../edit",
  "transcription_doc_id": "1ABCDEF...",
  "context": "Witness",
  "extra": { "foo": "bar" },
  "raw_text": "Texto literal de prueba",
  "language": "es|en",
  "output_doc_id": "1DOC_DESTINO...",
  "sheet_callback": {
    "spreadsheet_id": "1SPREADSHEET_ID...",
    "sheet_name": "Hoja 1",
    "row_index": 2,
    "testimony_doc_col": "H",
    "status_col": "J"
  }
}
```

#### Notas

* **Fuente**: usa *solo una* (idealmente), pero si vienen varias aplica la precedencia indicada.
* **Idioma**: controla selección de plantilla (si existe) o fallback (`es`/`en`).
* **`output_doc_id`**: opcional. Si falta, se crea uno nuevo vía ForgeDocs.
* **`sheet_callback`**: opcional. Si se incluye, actualiza la Google Sheet al finalizar con el link del documento y el estado.

### Response — `TestimonyResponse`

```json
{
  "status": "success",
  "message": "El resultado fue escrito correctamente en el documento.",
  "doc_id": "1DOC_DESTINO",
  "output_doc_link": "https://docs.google.com/document/d/1DOC_DESTINO/edit",
  "model": "gemini-2.5-flash",
  "language": "en",
  "case_id": "CASE-001",
  "request_id": null
}
```

---

## Configuración

Variables de entorno soportadas (archivo `.env` y/o `--set-env-vars`):

| Variable                         | Ejemplo                   | Descripción                           |
| -------------------------------- | ------------------------- | ------------------------------------- |
| `GOOGLE_CLOUD_PROJECT`           | `ortega-473114`           | ID de proyecto GCP                    |
| `VERTEX_LOCATION`                | `us-central1`             | Región de Vertex                      |
| `VERTEX_MODEL`                   | `gemini-2.5-flash`        | Modelo en Vertex                      |
| `LLM_BACKEND`                    | `vertex`                  | Backend LLM (prod: `vertex`)          |
| `DEFAULT_LANGUAGE`               | `es`                      | Idioma por defecto (`es`/`en`)        |
| `PROMPTS_DIR`                    | `/app/src/domain/prompts` | Carpeta de plantillas                 |
| `HEALTHCHECK_DOC_ID`             | `1ABC...`                 | Doc canario para `GET /health/sa`     |
| `SERVICE_ACCOUNT_EMAIL`          | `sa@project.iam.gserviceaccount.com` | Email de SA para mensajes de error |
| `LOG_LEVEL`                      | `INFO`                    | `DEBUG/INFO/WARNING/ERROR`            |
| `LOG_FORMAT`                     | `json`                    | `json` o `plain`                      |
| `LOG_INCLUDE_PII`                | `false`                   | Evita loggear datos sensibles         |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/abs/path/sa.json`       | **Solo local** (no usar en Cloud Run) |

> **Cloud Run**: no definas `GOOGLE_APPLICATION_CREDENTIALS`. Usa la identidad del servicio del despliegue.

### `.env.example`

```env
GOOGLE_CLOUD_PROJECT=ortega-473114
VERTEX_LOCATION=us-central1
VERTEX_MODEL=gemini-2.5-flash
LLM_BACKEND=vertex
DEFAULT_LANGUAGE=es
PROMPTS_DIR=/app/src/domain/prompts
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_INCLUDE_PII=false

# Health
HEALTHCHECK_DOC_ID=

# Service Account
SERVICE_ACCOUNT_EMAIL=drive-sheets@ortega-473114.iam.gserviceaccount.com

# Solo local
GOOGLE_APPLICATION_CREDENTIALS=./sa_key.json
```

---

## Ejecución local

1. **Compartir** los Docs de **fuente** (transcripción) y **destino** (salida/health) con la SA del JSON:
   `drive-sheets@ortega-473114.iam.gserviceaccount.com` (o la tuya).

2. Exporta credenciales de SA:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\ruta\a\credentials\service_account.json"
```

3. Levanta el servidor:

```bash
uvicorn src.main:app --reload --port 8080
```

4. Health:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health | ConvertTo-Json -Depth 6
Invoke-RestMethod "http://127.0.0.1:8080/health/sa?doc_id=1DOC_HEALTH" | ConvertTo-Json -Depth 6
```

---

## Despliegue en GCP (Artifact Registry + Cloud Run)

> Requiere: `gcloud` inicializado, proyecto activo y permisos para AR/Run.

Habilitar APIs (una sola vez):

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com docs.googleapis.com drive.googleapis.com
```

Build & push:

```powershell
$PROJECT_ID = "ortega-473114"
$REGION     = "us-central1"
$REPO       = "ai"
$SERVICE    = "testimonios"
$TAG        = (Get-Date -Format 'yyyyMMdd-HHmm')
$IMAGE      = "{0}-docker.pkg.dev/{1}/{2}/{3}:{4}" -f $REGION,$PROJECT_ID,$REPO,$SERVICE,$TAG

gcloud builds submit --tag $IMAGE
```

Deploy:

```powershell
$SA_EMAIL = "drive-sheets@$PROJECT_ID.iam.gserviceaccount.com"

gcloud run deploy $SERVICE `
  --image $IMAGE `
  --service-account=$SA_EMAIL `
  --region=$REGION `
  --platform=managed `
  --allow-unauthenticated `
  --memory=1Gi `
  --cpu=1 `
  --concurrency=60 `
  --timeout=300 `
  --min-instances=0 `
  --max-instances=20 `
  --set-env-vars=GOOGLE_CLOUD_PROJECT=$PROJECT_ID,VERTEX_LOCATION=$REGION,REGION=$REGION `
  --set-env-vars=LLM_BACKEND=vertex,VERTEX_MODEL=gemini-2.5-flash `
  --set-env-vars=DEFAULT_LANGUAGE=es,PROMPTS_DIR=/app/src/domain/prompts `
  --set-env-vars=LOG_LEVEL=INFO,LOG_FORMAT=json,LOG_INCLUDE_PII=false `
  --set-env-vars=HEALTHCHECK_DOC_ID=1DOC_HEALTH `
  --set-env-vars=SERVICE_ACCOUNT_EMAIL=$SA_EMAIL
```

Obtener URL:

```bash
gcloud run services describe testimonios --region us-central1 --format='value(status.url)'
```

---

## Pruebas rápidas

### Health SA con env por omisión

```powershell
Invoke-RestMethod "$BASE_URL/health/sa" | ConvertTo-Json -Depth 6
```

**Health SA con `doc_id` en query**

```powershell
Invoke-RestMethod "$BASE_URL/health/sa?doc_id=1DOC_HEALTH" | ConvertTo-Json -Depth 6
```

**Generación con transcripción en Doc (ES)**

```powershell
$body = @{
  case_id = "CASE-001"
  context = "Witness"
  language = "es"
  transcription_doc_id = "1DOC_TRANSCRIPCION"
  output_doc_id = "1DOC_SALIDA"
} | ConvertTo-Json -Depth 6

Invoke-RestMethod "$BASE_URL/generate-testimony" -Method Post -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 6
```

**Generación con `raw_text` (EN) + callback a Sheets**

```powershell
$body = @{
  case_id = "CASE-ENG-001"
  context = "Witness"
  language = "en"
  raw_text = "This is the literal transcript text..."
  output_doc_id = "1DOC_DESTINO"
  sheet_callback = @{
    spreadsheet_id = "1SPREADSHEET_ID"
    sheet_name = "Hoja 1"
    row_index = 2
    testimony_doc_col = "H"
    status_col = "J"
  }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod "$BASE_URL/generate-testimony" -Method Post -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 6
```

**Webhook Chain (para encadenamiento automático)**

```powershell
$body = @{
  case_id = "CASE-002"
  context = "Witness"
  transcription_doc_id = "1DOC_TRANSCRIPCION"
  output_doc_id = "1DOC_SALIDA"
} | ConvertTo-Json -Depth 6

Invoke-RestMethod "$BASE_URL/webhook/chain" -Method Post -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 6
```

---

## Permisos, APIs y SA

* **APIs** (proyecto): `run`, `artifactregistry`, `aiplatform`, `docs`, `drive`.
* **Service Account (ejecución en Cloud Run)**:

  * `roles/aiplatform.user` (Vertex).
  * Acceso a los **Docs** (fuente y destino) vía **compartir** como **Editor** o pertenecer a la **Shared Drive** con **Content Manager**/**Editor**.
  * Acceso a **Google Sheets** (si se usa callback) vía **compartir** como **Editor**.
* **Importante**: la SA **no crea** archivos. Siempre se **sobrescribe** un Doc existente.
* **Callback a Sheets**: si se incluye `sheet_callback` en el request, el servicio actualizará la Google Sheet con el link del documento generado y el estado final.

---

## Logging

* Formato `json` por defecto (configurable con `LOG_FORMAT`).
* Evita PII en logs (`LOG_INCLUDE_PII=false`).
* Eventos clave que se loggean:

  * Autenticación/ADC inicializada.
  * Fuente seleccionada (`raw_text`, `transcription_doc_id`, `transcription_link`).
  * Llamadas a Vertex (modelo/idioma).
  * Escritura a Docs (doc_id de salida).
  * Callback a Google Sheets (si aplica).
  * Errores (403/404/500) con mensajes accionables.

---

## Manejo de errores

Códigos estándar:

* **422**: validación/fuente ausente (`raw_text`/`transcription_doc_id`/`transcription_link`).
* **403**: permisos insuficientes (`"Comparte el Doc con la SA: drive-sheets@ortega-473114.iam.gserviceaccount.com"`).
* **404**: documento no encontrado/ID inválido.
* **500**: error interno (Vertex/Docs no esperado, timeouts, etc.).

Mensajes claros y accionables en JSON.

---

## Solución de problemas

* **`403 insufficientPermissions` (Docs/Drive)**
  El Doc existe, pero **no** está compartido con **la misma SA** que corre en Cloud Run:
  `gcloud run services describe testimonios --region us-central1 --format="value(spec.template.spec.serviceAccountName)"`
  Comparte con esa SA como **Editor** o agrega la SA a la **Shared Drive**.

* **`404 Publisher Model ... not found` (Vertex)**
  Cambia `VERTEX_MODEL` a uno disponible en la región, p. ej.:
  `gemini-2.5-flash` / `gemini-1.5-pro-002` (según habilitación).

* **“ALTS creds ignored…”**
  Aviso benigno en local (no GCP). Ignorable.

* **`health/sa` falla sin `doc_id`**
  Falta `HEALTHCHECK_DOC_ID` en env o el doc no está compartido.

* **No ves logs**
  Revisa `LOG_LEVEL`, y en Cloud Run → **Logs** de la revisión activa.

---

## Notas y buenas prácticas

* **Nunca** subas llaves a Cloud Run; usa identidad del servicio (ADC).
* En **local**, usa `GOOGLE_APPLICATION_CREDENTIALS` **solo** para pruebas.
* Siempre usa `supportsAllDrives=True` en llamadas Drive cuando aplique.
* Mantén plantillas en `src/domain/prompts/` (por idioma).
* Taggea imágenes con fecha/hora para facilitar **rollback**.
* Mantén el **nombre del servicio** y **región** para conservar la misma URL.
* El campo `output_doc_id` es **obligatorio** en todas las requests (no hay doc por defecto).
* Para encadenamiento automático, usa el endpoint `/webhook/chain`.
* El callback a Sheets es **opcional** pero útil para pipelines automatizados.

---

## Tecnologías principales

* **FastAPI** 0.115.2 - Framework web moderno y rápido
* **Pydantic** 2.9.2 - Validación de datos y esquemas
* **Google Cloud AI Platform** 1.70.0 - Vertex AI (Gemini)
* **Google API Python Client** 2.154.0 - Google Docs/Drive/Sheets
* **gspread** 6.1.4 - Cliente simplificado para Google Sheets
* **Uvicorn** 0.31.1 - Servidor ASGI de producción
* **Jinja2** 3.1.4 - Motor de plantillas para prompts

---

## Licencia

MIT (o la que decidas en `LICENCE`).

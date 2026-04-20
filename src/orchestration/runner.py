# src/orchestration/runner.py
from __future__ import annotations

import json
import re
from typing import Any, Dict

from fastapi import HTTPException

from src.logging_conf import get_logger
from src.settings import get_settings
# Importamos los nuevos esquemas
from src.domain.schemas import TestimonyRequest, TestimonyResponse, TranscriptionWebhookRequest
from src.clients.drive_client import assert_sa_has_access
from src.clients.gdocs_client import write_markdown_to_document
from src.clients.vertex_client import generate_text
from src.clients.sheets_client import update_row_status
from src.auth import build_drive_client
from src.domain.prompt_loader import render_testimony_prompt
from src.orchestration.strategies import DocumentStrategyFactory


logger = get_logger(__name__)
settings = get_settings()

def _extract_doc_id_from_url(url: str) -> str:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    if not m: raise HTTPException(422, "No pude extraer el doc_id.")
    return m.group(1)

def _resolve_language(req: TestimonyRequest) -> str:
    if req.language: return (req.language or "").lower()
    return (req.extra or {}).get("language", settings.default_language or "es").lower()

def _map_google_http_error(e: Exception, *, op: str, file_id: str) -> HTTPException:
    msg = f"❌ [Google Docs Error] {op} falló para ID '{file_id}'. Causa probable: Falta compartir con la SA o ID inválido. Detalle: {e}"
    logger.error(msg, extra={"file_id": file_id, "op": op})
    return HTTPException(status_code=403, detail=msg)

def _fallback_prompt(transcript: str, req: TestimonyRequest, language: str) -> str:
    return f"TRANSCRIPT:\n{transcript}\n\nCLIENT: {req.client}\nWITNESS: {req.witness}\nCONTEXT: {req.context}\nLANGUAGE: {language}"

# ---------------------------
# Caso de uso principal
# ---------------------------

def run_testimony(req: TestimonyRequest) -> Dict[str, Any]:
    """
    Ejecuta el flujo de generación de testimonio y escribe SIEMPRE en el Doc output_doc_id.
    """
    logger.info("🚀 run_testimony", extra={"case_id": req.case_id, "context": req.context})

    # 1. Validaciones y Accesos (o Creación Automática)
    target_doc_id = (req.output_doc_id or "").strip()
    
    if not target_doc_id:
        from src.clients.forgedocs_client import create_doc_via_forgedocs
        logger.info("🆕 'output_doc_id' no provisto. Llamando a ForgeDocs para crear uno nuevo...")
        try:
            doc_name = f"Testimonio - {req.case_id} - {req.witness or req.client or 'Sin Nombre'}"
            
            # 1. Definir editores (incluyendo SIEMPRE a esta Service Account para no perder acceso)
            editors = []
            sa_email = settings.service_account_email
            if sa_email:
                logger.info(f"🔑 Incluyendo SA '{sa_email}' como editor en ForgeDocs.")
                editors.append(sa_email)
            else:
                logger.warning("⚠️ No se pudo determinar el email de la SA. ForgeDocs podría no compartir el doc correctamente.")
            
            if req.extra and "client_email" in req.extra:
                editors.append(req.extra["client_email"])
            
            # 2. Llamar a ForgeDocs
            target_doc_id = create_doc_via_forgedocs(
                document_name=doc_name,
                editor_emails=editors,
                parent_folder_id=(req.extra or {}).get("parent_folder_id")
            )
        except Exception as e:
            logger.error(f"❌ Falló la creación automática de Doc vía ForgeDocs: {e}")
            raise HTTPException(500, detail="No se pudo crear el documento de destino.")

    try:
        # El destino SIEMPRE es un Google Doc (nativo)
        assert_sa_has_access(target_doc_id, use_docs_api=True)
    except Exception as e:
        raise _map_google_http_error(e, op="Validar acceso destino", file_id=target_doc_id)

    # 2. Obtener Fuente (Usando Strategy Pattern)
    language = _resolve_language(req)
    transcript = ""
    
    if req.raw_text:
        transcript = req.raw_text
    else:
        # Determinar ID del documento fuente
        src_doc_id = ""
        if req.transcription_doc_id:
            src_doc_id = req.transcription_doc_id.strip()
        elif req.transcription_link:
            src_doc_id = _extract_doc_id_from_url(str(req.transcription_link))
        
        if src_doc_id:
            try:
                # Factory elige la estrategia según el mimeType (Smart Reader)
                strategy = DocumentStrategyFactory.get_strategy(src_doc_id)
                strategy.validate_access(src_doc_id)
                transcript = strategy.read(src_doc_id)
            except Exception as e:
                raise _map_google_http_error(e, op="Leer fuente (Strategy)", file_id=src_doc_id)
        else:
            raise HTTPException(422, "Falta fuente (raw_text, id o link).")

    if not transcript or len(transcript.strip()) < 20:
        raise HTTPException(422, "Transcript vacío.")

    # 3. Prompt + LLM
    try:
        prompt = render_testimony_prompt(language=language, templates_dir=settings.prompts_dir, transcript=transcript, req=req)
    except Exception:
        logger.warning("⚠️ No se pudo renderizar el prompt con template. Usando fallback.")
        prompt = _fallback_prompt(transcript=transcript, req=req, language=language)

    try:
        output_text = generate_text(prompt)
    except Exception:
        raise HTTPException(500, "Error al generar texto con el modelo.")

    # 4. Escribir en Doc
    try:
        write_markdown_to_document(target_doc_id, output_text)
    except Exception as e:
        raise _map_google_http_error(e, op="Escribir salida", file_id=target_doc_id)

    # 5. Obtener link
    try:
        drive = build_drive_client()
        meta = drive.files().get(fileId=target_doc_id, fields="webViewLink", supportsAllDrives=True).execute()
        output_link = meta.get("webViewLink", f"https://docs.google.com/document/d/{target_doc_id}/edit")
    except Exception:
        output_link = f"https://docs.google.com/document/d/{target_doc_id}/edit"

    logger.info("✅ Testimonio generado", extra={"case_id": req.case_id})

    # 6. CALLBACK A GOOGLE SHEETS
    if req.sheet_callback:
        cb = req.sheet_callback
        logger.info(f"📊 Actualizando Sheet: {cb.spreadsheet_id} (Fila {cb.row_index})")
        
        try:
            # Escribir URL del Testimonio
            if cb.testimony_doc_col:
                update_row_status(
                    cb.spreadsheet_id, cb.sheet_name, cb.row_index, 
                    cb.testimony_doc_col, output_link
                )
            
            # Escribir Status Final
            if cb.status_col:
                 update_row_status(
                    cb.spreadsheet_id, cb.sheet_name, cb.row_index, 
                    cb.status_col, "✅ Testimonio Listo"
                )
        except Exception as e:
            logger.error(f"❌ Error actualizando Sheets: {e}")

    return TestimonyResponse(
        status="success",
        message="Testimonio generado correctamente.",
        doc_id=target_doc_id,
        output_doc_link=output_link,
        model=settings.model_id,
        language=language,
        case_id=req.case_id,
        request_id=req.request_id,
    ).model_dump()

def run_testimony_from_webhook(webhook_req: TranscriptionWebhookRequest) -> Dict[str, Any]:
    """
    Recibe el payload del Transcriptor, lo convierte a TestimonyRequest interno
    y ejecuta el proceso.
    """
    logger.info(f"🔗 Webhook recibido para Case: {webhook_req.case_id}")
    
    meta = webhook_req.metadata
    
    # Mapeo: Webhook -> Request Interno
    internal_req = TestimonyRequest(
        case_id=webhook_req.case_id,
        context=meta.context,
        client=meta.client_name,
        witness=meta.witness_name,
        transcription_doc_id=webhook_req.transcription_doc_id,
        output_doc_id=meta.output_doc_id,
        sheet_callback=meta.sheet_callback,
        language="es"
    )
    
    return run_testimony(internal_req)
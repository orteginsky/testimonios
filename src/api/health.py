# src/api/health.py
from __future__ import annotations

import os
from fastapi import APIRouter, HTTPException, Query

from src.logging_conf import get_logger
from src.settings import get_settings
from src.auth import build_docs_client
from src.orchestration.strategies import DocumentStrategyFactory, GoogleDocStrategy

logger = get_logger(__name__)
settings = get_settings()
router = APIRouter()


@router.get("/health", summary="Ping simple")
async def health():
    return {"ok": True, "service": "testimonios", "project": settings.project_id}


@router.get("/health/sa", summary="Verificación SA/ADC compatible con Google Docs y Word")
async def health_sa(doc_id: str | None = Query(default=None, description="ID de Doc o Docx para probar acceso")):
    """
    Valida el acceso de la Service Account a un documento.
    Si el documento es un Google Doc nativo, realiza una prueba de escritura reversible.
    Si es un archivo .docx, realiza una prueba de lectura de texto.
    """
    # 1) resolver doc para prueba
    test_doc_id = doc_id or os.getenv("HEALTHCHECK_DOC_ID")
    if not test_doc_id:
        raise HTTPException(status_code=422, detail="Falta doc_id en query o HEALTHCHECK_DOC_ID en el entorno.")

    # 2) Obtener estrategia y validar acceso (Smart Reader logic)
    try:
        strategy = DocumentStrategyFactory.get_strategy(test_doc_id)
        strategy.validate_access(test_doc_id)
    except Exception as e:
        raise HTTPException(status_code=403, detail=f"Sin acceso al documento {test_doc_id}: {e}")

    # 3) Prueba de funcionalidad según el tipo
    result_op = "unknown"
    try:
        if isinstance(strategy, GoogleDocStrategy):
            # Prueba de ESCRITURA reversible (típico para docs de salida)
            docs = build_docs_client()
            probe = f"SA health-check sync test: {os.uname().nodename}"
            docs.documents().batchUpdate(
                documentId=test_doc_id,
                body={"requests": [
                    {"insertText": {"location": {"index": 1}, "text": probe}},
                    {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 1 + len(probe)}}},
                ]}
            ).execute()
            result_op = "write_reversible_ok"
        else:
            # Prueba de LECTURA (típico para transcripciones .docx / source files)
            content = strategy.read(test_doc_id)
            if not content:
                raise ValueError("El documento parece estar vacío o no se pudo extraer texto legible.")
            result_op = f"read_ok ({len(content)} chars)"

    except Exception as e:
        logger.error(f"❌ Falló prueba de funcionalidad en health check para {test_doc_id}: {e}")
        raise HTTPException(status_code=403, detail=f"Fallo en prueba de {strategy.__class__.__name__}: {e}")

    # 4) Vertex AI init (sin generar contenido)
    from src.clients.vertex_client import init_vertex_ai
    try:
        init_vertex_ai()
    except Exception as e:
        raise HTTPException(status_code=403, detail=f"Vertex AI no inicializó: {e}")

    return {
        "status": "ok", 
        "doc_id": test_doc_id, 
        "strategy": strategy.__class__.__name__,
        "operation_test": result_op, 
        "vertex": "ok"
    }
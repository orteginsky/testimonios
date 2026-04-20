from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, Any
from io import BytesIO
from docx import Document
from googleapiclient.errors import HttpError

from src.clients.drive_client import get_file_metadata, export_doc_as_text, download_file_bytes
from src.logging_conf import get_logger
from src.auth import build_docs_client, build_drive_client

logger = get_logger(__name__)

class DocumentStrategy(ABC):
    @abstractmethod
    def read(self, document_id: str) -> str:
        """Extrae el contenido de texto del documento."""
        pass

    @abstractmethod
    def validate_access(self, document_id: str) -> None:
        """Verifica que la cuenta de servicio tenga acceso al documento."""
        pass

class GoogleDocStrategy(DocumentStrategy):
    def read(self, document_id: str) -> str:
        logger.info(f"📄 Leyendo Google Doc nativo: {document_id}")
        return export_doc_as_text(document_id)

    def validate_access(self, document_id: str) -> None:
        docs = build_docs_client()
        try:
            docs.documents().get(documentId=document_id).execute()
        except HttpError as e:
            logger.error(f"❌ [Google Docs Access] Error accediendo a {document_id}: {e}")
            raise

class WordDocStrategy(DocumentStrategy):
    def read(self, document_id: str) -> str:
        logger.info(f"📂 Leyendo archivo Microsoft Word (.docx): {document_id}")
        bytes_data = download_file_bytes(document_id)
        doc = Document(BytesIO(bytes_data))
        # Extraer texto de párrafos ignorando vacíos
        full_text = [para.text for para in doc.paragraphs if para.text.strip()]
        return "\n".join(full_text)

    def validate_access(self, document_id: str) -> None:
        drive = build_drive_client()
        try:
            drive.files().get(
                fileId=document_id,
                fields="id,name,mimeType",
                supportsAllDrives=True
            ).execute()
        except HttpError as e:
            logger.error(f"❌ [Word Doc Access] Error accediendo a {document_id}: {e}")
            raise

class GenericDriveStrategy(DocumentStrategy):
    """Fallback para otros tipos de archivos que Drive pueda exportar."""
    def read(self, document_id: str) -> str:
        logger.warning(f"⚠️ Intentando exportar archivo genérico a texto: {document_id}")
        return export_doc_as_text(document_id)

    def validate_access(self, document_id: str) -> None:
        drive = build_drive_client()
        drive.files().get(fileId=document_id, supportsAllDrives=True).execute()

class DocumentStrategyFactory:
    @staticmethod
    def get_strategy(document_id: str) -> DocumentStrategy:
        try:
            meta = get_file_metadata(document_id)
            mime = meta.get("mimeType", "")
            
            if mime == "application/vnd.google-apps.document":
                return GoogleDocStrategy()
            elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                return WordDocStrategy()
            else:
                logger.info(f"ℹ️ MimeType '{mime}' detectado. Usando estrategia genérica.")
                return GenericDriveStrategy()
        except HttpError as e:
            # Si ni siquiera podemos obtener metadatos, probablemente es un problema de permisos
            # o el ID no existe. Lanzamos para que el llamador lo maneje.
            logger.error(f"❌ No se pudo determinar la estrategia para {document_id}: {e}")
            raise

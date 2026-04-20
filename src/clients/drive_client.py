# src/clients/drive_client.py
from __future__ import annotations

import re
from io import BytesIO
from typing import Optional, Dict, Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from src.auth import build_drive_client, build_docs_client
from src.logging_conf import get_logger

logger = get_logger(__name__)

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

def find_file_in_folder_by_name(
    folder_id: str,
    name: str,
    mime_type: str,
    *,
    page_size: int = 10,
) -> Optional[dict]:
    """Busca por nombre y tipo dentro de una carpeta (incluye Shared Drives)."""
    drive = build_drive_client()
    q = (
        f"'{folder_id}' in parents and trashed=false "
        f"and mimeType='{mime_type}' and name='{name}'"
    )
    resp = drive.files().list(
        q=q,
        fields="files(id,name,mimeType,modifiedTime,owners)",
        pageSize=page_size,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None

def assert_sa_has_access(file_id: str, *, use_docs_api: bool = True) -> None:
    """
    Verifica que la Service Account actual pueda acceder al archivo.
    - Por defecto intenta Docs API si use_docs_api=True.
    - Si falla porque el documento no es un Google Doc nativo, reintenta con Drive API.
    """
    if use_docs_api:
        try:
            docs = build_docs_client()
            docs.documents().get(documentId=file_id).execute()
            return
        except HttpError as e:
            # Si el error es 400 "This operation is not supported for this document",
            # significa que es un archivo binario (ej. docx/pdf) y debemos usar Drive API.
            if e.resp.status == 400 and "not supported" in str(e).lower():
                logger.debug(f"ℹ️ {file_id} no es nativo de Google Docs. Reintentando con Drive API...")
            else:
                logger.error(f"[Docs Access] SA no puede acceder a {file_id}: {e}")
                raise

    drive = build_drive_client()
    try:
        drive.files().get(
            fileId=file_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        logger.error(f"[Drive Access] SA no puede acceder a {file_id}: {e}")
        raise

def grant_editor_to_sa(file_id: str, sa_email: str) -> None:
    """
    Otorga rol de editor a la SA sobre un archivo específico (si el caller tiene permisos).
    No envía notificación por email.
    """
    drive = build_drive_client()
    body = {"type": "user", "role": "writer", "emailAddress": sa_email}
    drive.permissions().create(
        fileId=file_id,
        body=body,
        sendNotificationEmail=False,
        supportsAllDrives=True,
    ).execute()
    logger.info(f"🔐 Se otorgó 'writer' a {sa_email} sobre {file_id}.")

# ------- Utilidades para PDFs/Drive --------

def parse_drive_url_to_id(url: str) -> str | None:
    """
    Extrae fileId de URLs tipo:
    https://drive.google.com/file/d/<FILE_ID>/view?...
    """
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)/", url)
    return m.group(1) if m else None

def download_file_bytes(file_id: str) -> bytes:
    """
    Descarga un archivo (binario) de Drive por fileId (útil para PDFs).
    """
    drive = build_drive_client()
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fd=fh, request=request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()

def get_file_metadata(file_id: str) -> dict:
    """Obtiene metadatos (id, name, mimeType) de un archivo en Drive."""
    drive = build_drive_client()
    return drive.files().get(
        fileId=file_id, 
        fields="id,name,mimeType", 
        supportsAllDrives=True
    ).execute()

def export_doc_as_text(file_id: str) -> str:
    """Exporta un Google Doc nativo a texto plano usando la Drive API."""
    drive = build_drive_client()
    content = drive.files().export(
        fileId=file_id, 
        mimeType='text/plain'
    ).execute()
    return content.decode('utf-8')


logger = get_logger(__name__)

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def create_google_doc_in_folder(folder_id: str, title: str) -> Dict[str, Any]:
    """
    Crea un Google Doc vacío dentro de una carpeta (incluye Shared Drives).
    Devuelve { 'id', 'name', 'webViewLink', 'driveId' }.
    """
    drive = build_drive_client()
    body = {
        "name": title,
        "mimeType": DOC_MIME,
        "parents": [folder_id],
    }
    file = drive.files().create(
        body=body,
        fields="id,name,webViewLink,driveId",
        supportsAllDrives=True,
    ).execute()
    logger.info(f"🆕 Doc creado: {file['name']} ({file['id']}) en folder {folder_id}")
    return file

# src/clients/drive_client.py

def ensure_folder_accessible(folder_id: str) -> dict:
    """
    Confirma que folder_id existe, es carpeta y la SA tiene acceso.
    Devuelve metadatos de la carpeta. Lanza HttpError en 403/404.
    Lanza ValueError si el ID no es de carpeta.
    """
    drive = build_drive_client()
    meta = drive.files().get(
        fileId=folder_id,
        fields="id,name,mimeType,webViewLink,driveId",
        supportsAllDrives=True,
    ).execute()
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        raise ValueError(
            f"El ID '{folder_id}' no es una carpeta (mimeType={meta.get('mimeType')})."
        )
    return meta

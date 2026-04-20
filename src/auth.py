# src/auth.py
from __future__ import annotations
import os
import certifi
os.environ["SSL_CERT_FILE"] = certifi.where()

from functools import lru_cache
from typing import Iterable, Optional, Tuple

import google.auth
from google.auth.credentials import Credentials as BaseCredentials
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

import vertexai

from src.settings import get_settings
from src.logging_conf import get_logger

logger = get_logger(__name__)

# --- SCOPES GLOBALES ---
# --- SCOPES GLOBALES ---
DRIVE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
)
DOCS_SCOPES = (
    "https://www.googleapis.com/auth/documents",
)
SHEETS_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
)
VERTEX_SCOPE = (
    "https://www.googleapis.com/auth/cloud-platform",
)

# ✅ Fusión de todos los scopes
WORKSPACE_SCOPES = tuple(sorted(set(DRIVE_SCOPES + DOCS_SCOPES + SHEETS_SCOPES + VERTEX_SCOPE)))

settings=get_settings()

# --- HELPERS ---
def _scopes_tuple(scopes: Optional[Iterable[str]]) -> Tuple[str, ...]:
    return tuple(sorted(set(scopes or [])))

def _from_service_account_file(path: str, scopes: Tuple[str, ...]) -> SACredentials:
    if not os.path.exists(path):
        raise FileNotFoundError(f"No se encontró el archivo de credenciales: {path}")
    creds = SACredentials.from_service_account_file(path, scopes=list(scopes))
    logger.info(f"🔑 Usando Service Account JSON: {path} (Email: {creds.service_account_email})")
    return creds

def _adc_credentials(scopes: Tuple[str, ...]) -> BaseCredentials:
    creds, _ = google.auth.default(scopes=list(scopes))
    logger.debug("Usando credenciales Application Default Credentials (ADC).")
    return creds

# --- CREDENCIALES CACHEADAS ---
@lru_cache(maxsize=4)
def get_workspace_credentials(scopes: Optional[Iterable[str]] = WORKSPACE_SCOPES) -> BaseCredentials:
    scopes_t = _scopes_tuple(scopes)
    # ✅ CAMBIO: sa_credentials_path (y no google_application_credentials)
    if settings.sa_credentials_path:
        return _from_service_account_file(settings.sa_credentials_path, scopes_t)
    return _adc_credentials(scopes_t)


# --- CLIENTES GOOGLE API ---
@lru_cache(maxsize=4)
def build_drive_client():
    creds = get_workspace_credentials(WORKSPACE_SCOPES)
    logger.info("📁 Cliente Drive inicializado (cacheado).")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# src/auth.py
@lru_cache(maxsize=4)
def build_docs_client():
    creds = get_workspace_credentials(WORKSPACE_SCOPES)
    logger.info("📄 Cliente Docs inicializado (cacheado).")
    return build("docs", "v1", credentials=creds, cache_discovery=False)


@lru_cache(maxsize=4)
def build_sheets_client():
    creds = get_workspace_credentials(WORKSPACE_SCOPES)
    logger.info("📊 Cliente Sheets inicializado (cacheado).")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# --- VERTEX AI ---
@lru_cache(maxsize=1)
def init_vertex_ai() -> bool:
    """
    Inicializa Vertex AI con las credenciales actuales (ADC en Cloud Run, SA JSON en local).
    """
    # ✅ Nombres correctos de settings
    project = settings.project_id
    location = settings.vertex_location
    logger.info(f"🤖 Inicializando Vertex AI (proyecto={project}, región={location})...")

    try:
        creds = get_workspace_credentials()
        try:
            # Algunos tipos de credenciales no requieren refresh; ignora si falla
            creds.refresh(Request())
        except Exception:
            pass

        # ✅ Una sola llamada a init
        vertexai.init(project=project, location=location, credentials=creds)
        logger.info("✅ Vertex AI inicializado correctamente.")
        return True

    except Exception as e:
        logger.error(f"Error al inicializar Vertex AI: {e}")
        raise

def get_all_clients() -> dict:
    """Devuelve un paquete de clientes Google + Vertex listos para usar."""
    init_vertex_ai()
    return {
        "drive": build_drive_client(),
        "docs": build_docs_client(),
        "sheets": build_sheets_client(),
        "vertex_initialized": True,
    }

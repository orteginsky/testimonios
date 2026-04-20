# src/settings.py
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

# Carga variables del .env (solo útil en local; en Cloud Run usa env vars del servicio)
load_dotenv()


def _first_env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Devuelve el primer valor de env definido en names, o default."""
    for n in names:
        v = os.getenv(n)
        if v is not None and v != "":
            return v
    return default


class Settings(BaseModel):
    # --- Proyecto / Región ---
    project_id: str = _first_env("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT_ID", default="")
    region: str = _first_env("REGION", "GCP_REGION", "VERTEX_LOCATION", default="us-central1")

    # --- Backend LLM ---
    # Producción recomendado: "vertex" (IAM). Opcional: "gemini_api".
    llm_backend: str = os.getenv("LLM_BACKEND", "vertex").lower()
    gemini_api_key: Optional[str] = os.getenv("GEMINI_API_KEY")  # usar solo si llm_backend == "gemini_api"

    # Vertex config
    vertex_model: str = os.getenv("VERTEX_MODEL", "")        # p.ej. "gemini-2.5-flash"
    vertex_model_id: str = os.getenv("VERTEX_MODEL_ID", "")  # alias histórico

    @property
    def model_id(self) -> str:
        return (self.vertex_model_id or self.vertex_model or "gemini-2.5-flash").strip()

    vertex_location: str = os.getenv("VERTEX_LOCATION", "us-central1")

    # --- Docs/Drive ---
    # Nota: NO hay creación de documentos. Solo escritura en un Doc provisto en el request.
    # Se mantiene opcionalmente el Shared Drive ID para llamadas con supportsAllDrives=True.
    # --- Integración ForgeDocs ---
    forgedocs_url: str = os.getenv("FORGEDOCS_URL", "https://forgedocs-357230337220.us-central1.run.app")
    # Carpeta por defecto si el request no trae una (ej: Carpeta de Testimonios del Proyecto)
    default_parent_folder_id: Optional[str] = os.getenv("DEFAULT_PARENT_FOLDER_ID")

    # --- Idioma/plantillas ---
    default_language: str = os.getenv("DEFAULT_LANGUAGE", "es")
    prompts_dir: Path = Path(
        os.getenv("PROMPTS_DIR", Path(__file__).resolve().parent / "domain" / "prompts")
    )
    
    # --- Service Account / Auth ---
    _service_account_email: str = os.getenv("SERVICE_ACCOUNT_EMAIL", "")

    @property
    def service_account_email(self) -> str:
        """Devuelve el email de la SA, intentando autodescubrimiento si no está definido."""
        if self._service_account_email:
            return self._service_account_email
        
        # Intento de autodescubrimiento en GCP (Metadata Server)
        try:
            import httpx
            # Timeout corto para no bloquear si no estamos en GCP
            resp = httpx.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
                timeout=1.0
            )
            if resp.status_code == 200:
                discovered = resp.text.strip()
                # Cacheamos el resultado en la instancia
                self._service_account_email = discovered
                return discovered
        except Exception:
            pass
        
        return ""
    # Solo LOCAL: ruta al JSON de la SA. En Cloud Run usa ADC (sin llaves).
    sa_credentials_path: Optional[str] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None

    # --- Controles heredados ---
    # Nunca usar OAuth en prod: mantener false (se conserva solo por compatibilidad).
    use_oauth: bool = os.getenv("USE_OAUTH", "false").lower() in {"true", "1", "yes"}

    # --- Logging ---
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()        # INFO | DEBUG | WARNING | ERROR
    log_format: str = os.getenv("LOG_FORMAT", "json").lower()      # json | text
    log_include_pii: bool = os.getenv("LOG_INCLUDE_PII", "false").lower() in {"true", "1", "yes"}

    # --- Utilidades ---
    @property
    def is_cloud_run(self) -> bool:
        # Cloud Run define K_SERVICE en tiempo de ejecución
        return bool(os.getenv("K_SERVICE"))

    @property
    def running_with_sa_file(self) -> bool:
        return bool(self.sa_credentials_path and Path(self.sa_credentials_path).exists())

    # ✅ Compat: este servicio NO requiere carpeta de salida ni defaults.
    # Mantengo la propiedad para evitar rompimientos en imports antiguos; siempre False.
    @property
    def requires_output_folder(self) -> bool:
        return False

    @field_validator("llm_backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        allowed = {"vertex", "gemini_api"}
        if v not in allowed:
            raise ValueError(f"LLM_BACKEND inválido: {v}. Usa uno de {allowed}")
        return v

    def sanity_warnings(self) -> list[str]:
        """
        Advertencias de configuración comunes (no detiene arranque).
        """
        warnings: list[str] = []

        if self.is_cloud_run and self.sa_credentials_path:
            warnings.append(
                "Cloud Run detectado (K_SERVICE) pero GOOGLE_APPLICATION_CREDENTIALS está definido. "
                "En Cloud Run usa ADC/Workload Identity (sin llaves) y elimina esa variable."
            )

        if not self.service_account_email:
            warnings.append(
                "SERVICE_ACCOUNT_EMAIL no está definido. Útil para mensajes de error accionables."
            )

        if self.use_oauth:
            warnings.append(
                "USE_OAUTH=true detectado. Este servicio usa exclusivamente Service Account (ADC)."
            )

        if self.llm_backend == "gemini_api" and not self.gemini_api_key:
            warnings.append(
                "LLM_BACKEND=gemini_api pero GEMINI_API_KEY no está configurado. Para prod, preferir LLM_BACKEND=vertex."
            )

        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()

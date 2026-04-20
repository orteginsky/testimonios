# src/domain/schemas.py
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from typing import Optional, Dict, Any, Literal

# --- 1. Configuración de Callback para Sheets ---
class SheetCallbackConfig(BaseModel):
    """Configuración para escribir el resultado en Google Sheets"""
    spreadsheet_id: str = Field(..., description="ID de la hoja de cálculo")
    sheet_name: str = Field(default="Hoja 1", description="Nombre de la pestaña")
    row_index: int = Field(..., description="Fila donde se escribirá el resultado")
    
    # Columnas específicas para este servicio
    testimony_doc_col: Optional[str] = Field(None, description="Columna para el link del Testimonio (ej: 'H')")
    status_col: Optional[str] = Field(None, description="Columna para status (ej: 'J')")

# --- 2. Estructuras para el Webhook (Input del Transcriptor) ---
class WebhookMetadata(BaseModel):
    """Datos que viajan dentro del campo 'metadata' del webhook del Transcriptor"""
    output_doc_id: str
    client_name: Optional[str] = None
    witness_name: Optional[str] = None
    context: str = "Witness"
    # Aquí recibimos la configuración de sheet para pasarla al proceso interno
    sheet_callback: Optional[SheetCallbackConfig] = None 

class TranscriptionWebhookRequest(BaseModel):
    """Payload estándar que envía el servicio de Transcripción al terminar"""
    transcription_doc_id: str
    doc_url: str
    case_id: str
    status: str
    metadata: WebhookMetadata 

# --- 3. Request Principal (Actualizado) ---
class TestimonyRequest(BaseModel):
    # Identificación y contexto
    case_id: str = Field(..., description="ID del caso (obligatorio).")
    context: str = Field(..., description="Ej: 'Witness', 'Reference Letter' (obligatorio).")
    language: Optional[Literal["es", "en"]] = Field(
        None, description="Idioma de salida. Si falta, se usa settings.default_language."
    )

    client: Optional[str] = Field(None, description="Nombre del cliente (opcional).")
    witness: Optional[str] = Field(None, description="Nombre del testigo (opcional).")

    # Fuente
    raw_text: Optional[str] = Field(None)
    transcription_doc_id: Optional[str] = Field(None)
    transcription_link: Optional[HttpUrl] = Field(None)

    # Destino
    output_doc_id: Optional[str] = Field(
        None, 
        description="ID del Google Doc de salida. Si se omite, se creará uno nuevo automáticamente."
    )

    # Extras
    extra: Optional[Dict[str, Any]] = Field(default=None)
    request_id: Optional[str] = Field(default=None)

    # ✅ NUEVO: Campo para recibir la configuración del Callback
    sheet_callback: Optional[SheetCallbackConfig] = Field(
        None, 
        description="Si se incluye, se actualizará la Google Sheet al finalizar."
    )

    model_config = {"extra": "allow"}

    # --- Validaciones (Sin cambios) ---
    @field_validator("output_doc_id", mode="before")
    @classmethod
    def _strip_and_require_output(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return v
        s = str(v).strip()
        # Si es un string vacío, lo tratamos como None para disparar la creación automática
        if not s: return None
        return s

    @model_validator(mode="after")
    def _require_at_least_one_source(self):
        if not (self.raw_text or self.transcription_doc_id or self.transcription_link):
            raise ValueError(
                "Debes enviar una fuente: 'raw_text' o 'transcription_doc_id' o 'transcription_link'."
            )
        return self

class TestimonyResponse(BaseModel):
    status: str
    message: str
    doc_id: str
    output_doc_link: str
    model: str
    language: str
    case_id: str
    request_id: Optional[str] = None
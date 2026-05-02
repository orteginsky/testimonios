import httpx
import google.auth
import google.auth.transport.requests
from typing import Optional, List
from src.logging_conf import get_logger
from src.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

def get_identity_token(audience: str) -> str:
    """Obtiene un ID Token para autenticar llamadas entre microservicios en GCP."""
    # En local esto puede fallar si no tienes configurado el ADC correctamente para ID tokens
    auth_req = google.auth.transport.requests.Request()
    # Importar google.oauth2.id_token solo si es necesario para evitar dependencias circulares
    from google.oauth2 import id_token
    
    # ⚠️ Esto asume que estamos corriendo en un entorno con ADC (Cloud Run)
    # Si estamos en local, esto lanzará una excepción.
    creds, _ = google.auth.default()
    creds.refresh(auth_req)
    
    # Cloud Run espera un ID Token con la audience del servicio destino
    # Pero para simplificar en este entorno, a veces se usa el access token si la seguridad es distinta.
    # ForgeDocs espera un Bearer Token.
    return creds.token

def create_doc_via_forgedocs(
    document_name: str,
    editor_emails: List[str] = None,
    parent_folder_id: Optional[str] = None
) -> str:
    """
    Llama al microservicio ForgeDocs para crear un nuevo documento con los permisos correctos.
    Retorna el document_id.
    """
    url = f"{settings.forgedocs_url.rstrip('/')}/generate"
    
    payload = {
        "document_name": document_name,
        "editor_emails": editor_emails or [],
        "requesting_service": "testimonios-service",
        "parent_folder_id": parent_folder_id or settings.default_parent_folder_id,
        "share_with_workspace": True
    }
    
    logger.info(f"calling ForgeDocs to create doc: {document_name}")
    
    try:
        # Obtenemos token (esto puede variar según la configuración de IAM de Cloud Run)
        # Si el servicio es privado (--no-allow-unauthenticated), necesitamos el ID Token.
        # headers = {"Authorization": f"Bearer {get_identity_token(url)}"}
        
        # Por ahora intentamos sin auth o asumiendo que el transport maneja auth si está en la misma red,
        # pero lo correcto en GCP es usar el ID Token.
        
        # Nota: gcloud auth print-identity-token es la forma manual. 
        # En Python usamos google-auth para obtenerlo.
        
        with httpx.Client(timeout=30.0) as client:
            # En muchos despliegues internos de Cloud Run, si no hay IAP, basta con llamar a la URL.
            # Pero el README de ForgeDocs dice que espera un Bearer Token.
            
            # Intentamos obtener un ID Token (requerido para Cloud Run privado)
            auth_req = google.auth.transport.requests.Request()
            token = None
            try:
                # fetch_id_token solo funciona si estamos en GCP o con una SA que pueda firmar
                token = google.oauth2.id_token.fetch_id_token(auth_req, settings.forgedocs_url)
            except Exception as e:
                logger.debug(f"ℹ️ No se pudo obtener ID Token ({e}). Intentando con Access Token...")
                try:
                    creds, _ = google.auth.default()
                    creds.refresh(auth_req)
                    token = creds.token
                except Exception as e2:
                    logger.warning(f"⚠️ Tampoco se pudo obtener Access Token: {e2}")

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            
            data = resp.json()
            doc_id = data.get("document_id")
            logger.info(f"✅ ForgeDocs creó el documento: {doc_id}")
            return doc_id
            
    except Exception as e:
        logger.error(f"❌ Error al llamar a ForgeDocs: {e}")
        raise

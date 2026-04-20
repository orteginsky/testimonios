# src/clients/gdocs_client.py
from __future__ import annotations
import time
import random
import socket
import ssl
import json
import re

from typing import Any, Dict, List, Optional, TypedDict, cast, Iterator, Tuple
from http.client import IncompleteRead


from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from src.auth import build_docs_client
from src.clients.drive_client import (
    create_google_doc_in_folder, get_file_metadata, download_file_bytes, export_doc_as_text
)
from src.logging_conf import get_logger

logger = get_logger(__name__)

# ... (tipos y helpers se quedan)

def insert_text_into_empty_document(document_id: str, text: str) -> None:
    """
    Inserta `text` en un documento NUEVO (vacío) sin leer ni borrar previamente.
    Usar cuando acabas de crearlo con Drive.
    """
    docs = build_docs_client()

    MAX_CHARS = 80000
    if len(text) <= MAX_CHARS:
        body = {"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}
        req: HttpRequest = docs.documents().batchUpdate(documentId=document_id, body=body)
        _execute_with_retries(req)
        return

    # chunking
    start, part = 0, 1
    while start < len(text):
        chunk = text[start:start + MAX_CHARS]
        body = {"requests": [{"insertText": {"location": {"index": 1}, "text": chunk}}]}
        req: HttpRequest = docs.documents().batchUpdate(documentId=document_id, body=body)
        _execute_with_retries(req)
        logger.info(f"✍️ Insertado chunk {part} ({len(chunk)} chars)")
        start += MAX_CHARS
        part += 1
        time.sleep(0.15)


def create_and_fill_document(folder_id: str, title: str, text: str) -> Dict[str, Any]:
    """
    Crea un Doc nuevo en `folder_id` y escribe `text`. Devuelve dict con id y webViewLink.
    """
    file_info = create_google_doc_in_folder(folder_id=folder_id, title=title)
    insert_text_into_empty_document(file_info["id"], text)
    return file_info


# ========= Tipos (Google Docs API) =========

class StructuralElement(TypedDict, total=False):
    startIndex: int
    endIndex: int
    # Los siguientes campos pueden o no estar:
    paragraph: Dict[str, Any]
    table: Dict[str, Any]
    sectionBreak: Dict[str, Any]
    tableOfContents: Dict[str, Any]

class Body(TypedDict, total=False):
    content: List[StructuralElement]

class Document(TypedDict, total=False):
    title: str
    body: Body


def _extract_reason(err: HttpError) -> str:
    try:
        data = err.error_details or err.content or b""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="ignore")
        j = json.loads(data)
        return j.get("error", {}).get("errors", [{}])[0].get("reason", "") or \
               j.get("error", {}).get("status", "")
    except Exception:
        return ""

# ========= Reintentos genéricos =========

_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

def _is_ssl_eof(e: BaseException) -> bool:
    msg = str(e).lower()
    return "eof occurred in violation of protocol" in msg or "tlsv" in msg

def _execute_with_retries(request: HttpRequest, *, max_retries: int = 6) -> Optional[Dict[str, Any]]:
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            return cast(Dict[str, Any], request.execute(num_retries=0))
        except (IncompleteRead, ConnectionResetError, BrokenPipeError,
                ssl.SSLError, socket.timeout, OSError) as e:
            # Errores de transporte (incluye TLS/EOF)
            if attempt == max_retries:
                raise
            sleep = delay + random.uniform(0, delay * 0.5)
            kind = "SSL/EOF" if isinstance(e, ssl.SSLError) or _is_ssl_eof(e) else "RED"
            logger.warning(f"🔁 Retry {attempt}/{max_retries} por {kind}: {e}. Esperando {sleep:.1f}s…")
            time.sleep(sleep)
            delay = min(delay * 2, 20)
            # ⚠️ Para intentos altos, re-crea el cliente (por si la sesión quedó “sucia”)
            if attempt >= 3:
                from src.auth import build_docs_client
                _ = build_docs_client.cache_clear()  # limpia cache lru si usas Python 3.9+, si no, ignóralo
                build_docs_client()  # reconstruye cliente
            continue
        except HttpError as e:
            status = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
            if status in _RETRY_STATUSES and attempt < max_retries:
                sleep = delay + random.uniform(0, delay * 0.5)
                logger.warning(f"🔁 Retry {attempt}/{max_retries} por HttpError {status}: {e}. Esperando {sleep:.1f}s…")
                time.sleep(sleep)
                delay = min(delay * 2, 20)
                continue
            raise

# --- LECTURA DE CONTENIDO (tipado + reintentos) ---

# --- LECTURA DE CONTENIDO (Smart Reader: Docs y Docx) ---

def get_document_content(document_id: str) -> str:
    """
    Detecta el tipo de archivo y extrae el texto plano.
    Sustituye la lectura pesada de Docs API por Drive Export (más rápido)
    y añade soporte para archivos .docx de Office.
    """
    try:
        meta = get_file_metadata(document_id)
        mime = meta.get("mimeType", "")
        
        # 1. Google Doc Nativo
        if mime == "application/vnd.google-apps.document":
            logger.info(f"📄 Leyendo Google Doc nativo: {meta.get('name')} ({document_id})")
            return export_doc_as_text(document_id)
            
        # 2. Archivo Word (.docx)
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            logger.info(f"📂 Leyendo archivo Microsoft Word: {meta.get('name')} ({document_id})")
            from io import BytesIO
            from docx import Document
            
            bytes_data = download_file_bytes(document_id)
            doc = Document(BytesIO(bytes_data))
            # Extraer texto de párrafos
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text)
            return "\n".join(full_text)
            
        # 3. Otros (intentar exportar directamente)
        else:
            logger.warning(f"⚠️ MimeType '{mime}' no es nativo Doc/Docx. Intentando exportación a texto...")
            return export_doc_as_text(document_id)

    except HttpError as e:
        logger.error(f"❌ Error de Google API al leer {document_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Error inesperado leyendo {document_id}: {e}")
        raise

# ========= Helpers tipados =========

def _get_end_index(doc: Document) -> int:
    """
    Devuelve el endIndex del último StructuralElement del documento.
    Maneja casos donde body/content no existe.
    """
    body = cast(Body, doc.get("body", {}))
    content = cast(List[StructuralElement], body.get("content", []))
    if not content:
        # Si no hay contenido, por seguridad devolvemos 1 (inicio del doc)
        return 1
    last = content[-1]
    # endIndex puede faltar por tipado "total=False"; proveemos fallback seguro
    return int(last.get("endIndex", 1))

# ========= Operaciones de escritura =========

def write_to_document(document_id: str, text: str) -> None:
    """
    Borra el contenido (sin tocar el newline final) e inserta `text` al inicio.
    Seguro con retries y chunking.
    """
    MAX_CHARS = 50000  # ⬅️ de 80k a 50k
    docs = build_docs_client()

    # 1) Obtener endIndex
    get_req: HttpRequest = docs.documents().get(documentId=document_id)
    doc_raw: Optional[Dict[str, Any]] = _execute_with_retries(get_req)
    doc: Document = cast(Document, doc_raw)
    end_index: int = _get_end_index(doc)  # p.ej. 123

    # ⚠️ Importante: NO borrar el newline final del segmento raíz
    # Si el doc tiene contenido, end_index >= 2; borramos hasta end_index - 1
    delete_end = max(1, end_index - 1)

    # 2) Delete all (solo si hay algo que borrar)
    if delete_end > 1:
        delete_req: HttpRequest = docs.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": 1, "endIndex": delete_end}
                        }
                    }
                ]
            },
        )
        _execute_with_retries(delete_req)

    # 3) Insert (en chunks para evitar timeouts/payloads grandes)
    MAX_CHARS = 80000
    if len(text) <= MAX_CHARS:
        insert_body: Dict[str, Any] = {
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": text}}
            ]
        }
        insert_req: HttpRequest = docs.documents().batchUpdate(
            documentId=document_id, body=insert_body
        )
        _execute_with_retries(insert_req)
    else:
        start = 0
        part = 1
        while start < len(text):
            chunk = text[start:start + MAX_CHARS]
            insert_body = {"requests": [{"insertText": {"location": {"index": 1}, "text": chunk}}]}
            insert_req: HttpRequest = docs.documents().batchUpdate(documentId=document_id, body=insert_body)
            _execute_with_retries(insert_req)
            logger.info(f"✍️ Insertado chunk {part} ({len(chunk)} chars)")
            start += MAX_CHARS
            part += 1
            time.sleep(0.15)  # ⬅️ 150ms para no “aplanar” el backend


# ----------------------------
# Utilidades de parsing simple
# ----------------------------

_BOLD_RE   = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")  # evita **negritas**
_LINK_RE   = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HR_RE     = re.compile(r"^(\*\s*\*\s*\*|-{3,}|_{3,})\s*$")
_ATX_H_RE  = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE     = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_OL_RE     = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_CODEFENCE_RE = re.compile(r"^```")

# ----------------------------
# Lotes para batchUpdate
# ----------------------------

def _flush_requests(docs, document_id: str, requests: List[Dict[str, Any]]):
    if not requests:
        return
    body = {"requests": requests}
    req: HttpRequest = docs.documents().batchUpdate(documentId=document_id, body=body)
    _execute_with_retries(req)
    requests.clear()

# ----------------------------
# Borrado seguro (conserva \n final)
# ----------------------------

def _clear_document_keep_trailing_newline(docs, document_id: str):
    get_req: HttpRequest = docs.documents().get(documentId=document_id)
    doc_raw: Optional[Dict[str, Any]] = _execute_with_retries(get_req)
    doc = cast(Dict[str, Any], doc_raw)
    end_index: int = _get_end_index(doc)
    delete_end = max(1, end_index - 1)
    if delete_end > 1:
        del_req: HttpRequest = docs.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": delete_end}}}
                ]
            },
        )
        _execute_with_retries(del_req)
    # devolvemos el índice actual para empezar a insertar
    return 1  # insert at beginning

# ----------------------------
# Helpers de estilos inline
# ----------------------------

def _apply_inline_styles(docs, document_id: str, base_index: int, paragraph_text: str, requests: List[Dict[str, Any]]):
    """
    Aplica negritas, cursivas y links sobre el texto recién insertado.
    base_index: índice de inicio del párrafo (en el doc) justo antes del insert de este párrafo.
    Retorna length total del texto insertado (para avanzar el cursor).
    """
    # Primero convertimos [texto](url) a "texto" y guardamos rangos
    link_spans: List[Tuple[int, int, str]] = []
    out = []
    i = 0
    for m in _LINK_RE.finditer(paragraph_text):
        start, end = m.span()
        text_label, url = m.group(1), m.group(2)
        # texto previo
        out.append(paragraph_text[i:start])
        # el texto visible
        link_start = sum(len(x) for x in out)
        out.append(text_label)
        link_end = link_start + len(text_label)
        link_spans.append((link_start, link_end, url))
        i = end
    out.append(paragraph_text[i:])
    normalized = "".join(out)

    # Detectamos negritas y cursivas sobre "normalized"
    # Guardamos y luego quitamos los marcadores para calcular rangos limpios
    spans_bold: List[Tuple[int,int]] = []
    spans_italic: List[Tuple[int,int]] = []

    def _collect_spans(pattern: re.Pattern, text: str) -> Tuple[str, List[Tuple[int,int]]]:
        spans = []
        result = []
        shift = 0
        last = 0
        for m in pattern.finditer(text):
            s, e = m.span()
            content = m.group(1)
            # previo
            result.append(text[last:s])
            start_idx = sum(len(x) for x in result)
            result.append(content)
            end_idx = start_idx + len(content)
            spans.append((start_idx, end_idx))
            last = e
        result.append(text[last:])
        return "".join(result), spans

    normalized, spans_bold = _collect_spans(_BOLD_RE, normalized)
    normalized, spans_italic = _collect_spans(_ITALIC_RE, normalized)

    # Insertamos el texto ya limpio de marcadores + salto de línea
    text_with_newline = normalized + "\n"
    requests.append({"insertText": {"location": {"index": base_index}, "text": text_with_newline}})
    # Rango del párrafo insertado (sin contar el \n al estilizar inline)
    start_idx = base_index
    end_idx = base_index + len(normalized)

    # Links
    for (ls, le, url) in link_spans:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start_idx + ls, "endIndex": start_idx + le},
                "textStyle": {"link": {"url": url}},
                "fields": "link"
            }
        })

    # Negritas
    for (bs, be) in spans_bold:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start_idx + bs, "endIndex": start_idx + be},
                "textStyle": {"bold": True},
                "fields": "bold"
            }
        })

    # Cursivas
    for (is_, ie) in spans_italic:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start_idx + is_, "endIndex": start_idx + ie},
                "textStyle": {"italic": True},
                "fields": "italic"
            }
        })

    # devolvemos el avance total (texto + \n)
    return len(text_with_newline)

# ----------------------------
# Listas (bullets / números)
# ----------------------------

def _apply_list_bullets(requests: List[Dict[str, Any]], list_start_idx: int, list_end_idx: int, ordered: bool):
    preset = "NUMBERED_DECIMAL_ALPHA_ROMAN" if ordered else "BULLET_DISC_CIRCLE_SQUARE"
    requests.append({
        "createParagraphBullets": {
            "range": {"startIndex": list_start_idx, "endIndex": list_end_idx},
            "bulletPreset": preset
        }
    })

# ----------------------------
# Encabezados
# ----------------------------

def _apply_heading_style(requests: List[Dict[str, Any]], start_idx: int, end_idx: int, level: int):
    level = max(1, min(level, 6))
    requests.append({
        "updateParagraphStyle": {
            "range": {"startIndex": start_idx, "endIndex": end_idx},
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
            "fields": "namedStyleType"
        }
    })

# ----------------------------
# Escribir Markdown → Google Doc
# ----------------------------

def write_markdown_to_document(document_id: str, markdown_text: str) -> None:
    """
    Convierte un subset útil de Markdown a formato nativo de Google Docs:
    - #..###### → HEADING_X
    - listas -,*,+ → bullets
    - listas numeradas 1. 2. → numbered
    - **bold**, *italic*, [link](url)
    - --- / *** → horizontal rule
    - párrafos normales
    Maneja lotes y respeta newline terminal del doc.
    """
    docs = build_docs_client()
    cursor = _clear_document_keep_trailing_newline(docs, document_id)

    # Normalización básica
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    requests: List[Dict[str, Any]] = []
    batch_size = 0
    BATCH_LIMIT = 180  # operaciones por flush (ajusta si hace falta)

    i = 0
    N = len(lines)

    while i < N:
        line = lines[i]

        # 1) Regla horizontal
        if _HR_RE.match(line.strip()):
            requests.append({"insertHorizontalRule": {"location": {"index": cursor}}})
            # \n de separación para que el siguiente párrafo no se pegue
            requests.append({"insertText": {"location": {"index": cursor}}, "text": "\n"})
            cursor += 1
            batch_size += 2

        # 2) Fenced code blocks: por simplicidad los pegamos como texto monoespaciado
        elif _CODEFENCE_RE.match(line.strip()):
            i += 1
            code_lines = []
            while i < N and not _CODEFENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            code_text = "\n".join(code_lines)

            # Insertamos el bloque y luego aplicamos fuente monoespaciada
            text_len = len(code_text) + 1  # +\n
            requests.append({"insertText": {"location": {"index": cursor}, "text": code_text + "\n"}})
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor, "endIndex": cursor + len(code_text)},
                    "textStyle": {"weightedFontFamily": {"fontFamily": "Roboto Mono"}},
                    "fields": "weightedFontFamily"
                }
            })
            cursor += text_len
            batch_size += 2

        # 3) Encabezados ATX
        elif (m := _ATX_H_RE.match(line)):
            hashes, title = m.group(1), m.group(2).strip()
            level = len(hashes)
            # insert + estilo heading
            inserted = _apply_inline_styles(docs, document_id, cursor, title, requests)
            _apply_heading_style(requests, cursor, cursor + inserted - 1, level)  # -1 para no incluir \n
            cursor += inserted
            batch_size += 2 + 10  # aprox (inline styles generan varias ops)

        # 4) Listas (bloque contiguo UL/OL)
        elif _UL_RE.match(line) or _OL_RE.match(line):
            # Detecta el bloque completo de lista
            list_lines: List[Tuple[str, bool]] = []  # (texto, ordered?)
            ordered_block = bool(_OL_RE.match(line))
            j = i
            while j < N and ( _UL_RE.match(lines[j]) or _OL_RE.match(lines[j]) ):
                lm = _UL_RE.match(lines[j])
                if lm:
                    text_item = lm.group(3).strip()
                    list_lines.append((text_item, False))
                else:
                    lm = _OL_RE.match(lines[j])
                    text_item = lm.group(3).strip()
                    list_lines.append((text_item, True))
                j += 1

            list_start_idx = cursor
            # insertamos cada ítem como un párrafo (luego se convierte a bullet/numbered)
            for (item_text, is_ordered) in list_lines:
                inserted = _apply_inline_styles(docs, document_id, cursor, item_text, requests)
                cursor += inserted
                batch_size += 1 + 8  # aproximado
                ordered_block = ordered_block or is_ordered

            list_end_idx = cursor - 1  # antes del \n final del último item
            _apply_list_bullets(requests, list_start_idx, list_end_idx, ordered=ordered_block)
            batch_size += 1

            i = j - 1  # -1 porque al final del loop haremos i += 1

        # 5) Párrafo normal (incluye líneas vacías → saltos)
        else:
            text_line = line if line.strip() != "" else ""
            inserted = _apply_inline_styles(docs, document_id, cursor, text_line, requests)
            cursor += inserted
            batch_size += 1 + 6  # aprox

        # Flush si excede el límite
        if batch_size >= BATCH_LIMIT:
            _flush_requests(docs, document_id, requests)
            batch_size = 0
            time.sleep(0.1)

        i += 1

    # Flush final
    _flush_requests(docs, document_id, requests)
    logger.info("✅ Markdown renderizado con formato nativo de Google Docs.")

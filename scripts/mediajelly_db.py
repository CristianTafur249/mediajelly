#!/usr/bin/env python3
"""
Persistencia ligera para MediaJelly usando SQLite (WAL mode).

Provee funciones de conveniencia para reemplazar archivos de estado:
- mark_completed(path, label)
- is_completed(path) -> bool
- add_pending(path)
- remove_pending(path)
- get_pending_files() -> List[Path]
- save_progress(data: dict)
- load_progress() -> dict

Todas las operaciones usan transacciones y activan WAL para concurrencia.
Incluye reintentos automáticos ante bloqueos de base de datos.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple
from functools import wraps

try:
    from mediajelly_identity import fingerprint, is_settled
except ImportError:  # importado como paquete (scripts.mediajelly_db)
    from .mediajelly_identity import fingerprint, is_settled

DB_LOCK = threading.Lock()
MIGRATION_PENDING_MESSAGE = "Migración pendiente: ejecute mediajelly_migrate.py --apply"
SCHEMA_VERSION = 4
# Avisos que el panel muestra en "A revisar" y que Telegram notifica una vez.
TIPOS_AVISO = ("audio_desfasado", "revisar_subtitulo")
MAX_ATTEMPTS = 3
FAILURE_WINDOW_HOURS = 24
# Días de espera antes de volver a pasar por Whisper un archivo terminado
# cuyo idioma no se pudo detectar
LANGUAGE_RETRY_DAYS = 7

logger = logging.getLogger(__name__)


def retry_on_locked(max_retries=5, delay=1, backoff=2):
    """
    Decorador para reintentar operaciones SQLite cuando la base de datos está bloqueada.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        retries += 1
                        wait = delay * (backoff ** (retries - 1))
                        time.sleep(wait)
                    else:
                        raise
            raise Exception(f"Max retries exceeded for {func.__name__}")
        return wrapper
    return decorator


def _get_db_path() -> Path:
    base = Path(__file__).parent / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "mediajelly.db"


def _get_conn() -> sqlite3.Connection:
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    # Enable WAL and ensure foreign keys
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Inicializa el esquema si no existe."""
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS completed_files (
                    path TEXT PRIMARY KEY,
                    label TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_files (
                    path TEXT PRIMARY KEY,
                    label TEXT,
                    added_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS progress (
                    key TEXT PRIMARY KEY,
                    data TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_sessions (
                    chat_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_downloads (
                    file_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    file_name TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            _create_media_state_schema(conn)
        conn.close()


def _create_media_state_schema(conn: sqlite3.Connection) -> None:
    """Tablas del estado de medios identificado por huella (esquema v2)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_files (
            fingerprint TEXT PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            first_failed_at TEXT,
            last_error TEXT,
            failure_type TEXT,
            derived_from TEXT,
            missing_since TEXT,
            status_before_missing TEXT,
            created_at TEXT,
            updated_at TEXT,
            prioridad INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Bases creadas antes de la v3 no tienen la columna: se agrega sin perder filas
    columnas = {r[1] for r in conn.execute("PRAGMA table_info(media_files)")}
    if "prioridad" not in columnas:
        try:
            conn.execute("ALTER TABLE media_files ADD COLUMN prioridad INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():  # otro proceso ya la agregó
                raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_files_status ON media_files(status)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stream_languages (
            fingerprint TEXT NOT NULL,
            stream_index INTEGER NOT NULL,
            lang TEXT NOT NULL,
            source TEXT,
            PRIMARY KEY (fingerprint, stream_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retag_failures (
            fingerprint TEXT PRIMARY KEY,
            failed_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avisos (
            fingerprint TEXT NOT NULL,
            tipo TEXT NOT NULL,
            detalle TEXT,
            creado TEXT,
            PRIMARY KEY (fingerprint, tipo)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subtitle_queue (
            fingerprint TEXT PRIMARY KEY,
            added_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fingerprint_cache (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            fingerprint TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS language_attempts (
            fingerprint TEXT PRIMARY KEY,
            attempted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS panel_tareas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            argumentos TEXT NOT NULL,
            estado TEXT NOT NULL,
            salida TEXT,
            creada TEXT,
            iniciada TEXT,
            terminada TEXT
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if row[0] is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] < SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


@retry_on_locked()
def save_session(chat_id: str, session_data: dict) -> None:
    """Guarda o actualiza una sesión interactiva activa del bot."""
    init_db()
    payload = dict(session_data or {})
    payload["updated_at"] = time.time()
    serialized = json.dumps(payload, ensure_ascii=False)
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "REPLACE INTO bot_sessions(chat_id, data, updated_at) VALUES (?, ?, datetime('now'))",
                (str(chat_id), serialized),
            )
        conn.close()


@retry_on_locked()
def load_sessions() -> Dict[str, dict]:
    """Carga todas las sesiones activas y devuelve un dict {chat_id: data}."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute("SELECT chat_id, data FROM bot_sessions ORDER BY updated_at DESC").fetchall()
        conn.close()

    sessions: Dict[str, dict] = {}
    for row in rows:
        try:
            data = json.loads(row["data"])
            if isinstance(data, dict):
                sessions[str(row["chat_id"])] = data
        except Exception:
            continue
    return sessions


@retry_on_locked()
def delete_session(chat_id: str) -> None:
    """Elimina una sesión activa para un chat."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute("DELETE FROM bot_sessions WHERE chat_id = ?", (str(chat_id),))
        conn.close()


@retry_on_locked()
def save_download(chat_id: str, file_id: str, local_path: str, file_name: str, status: str = "downloading") -> None:
    """Registra o actualiza un archivo pendiente de organización."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO bot_downloads(file_id, chat_id, local_path, file_name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(file_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    local_path = excluded.local_path,
                    file_name = excluded.file_name,
                    status = excluded.status,
                    updated_at = datetime('now')
                """,
                (str(file_id), str(chat_id), str(local_path), str(file_name), status),
            )
        conn.close()


@retry_on_locked()
def update_download_status(file_id: str, status: str) -> None:
    """Actualiza el estado de una descarga en la base de datos."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "UPDATE bot_downloads SET status = ?, updated_at = datetime('now') WHERE file_id = ?",
                (status, str(file_id)),
            )
        conn.close()


@retry_on_locked()
def get_pending_downloads(chat_id: Optional[str] = None) -> List[dict]:
    """Devuelve las descargas pendientes o no organizadas, opcionalmente filtradas por chat."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        if chat_id is not None:
            rows = conn.execute(
                "SELECT file_id, chat_id, local_path, file_name, status, created_at, updated_at FROM bot_downloads WHERE chat_id = ? AND status != 'organized' ORDER BY updated_at DESC",
                (str(chat_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_id, chat_id, local_path, file_name, status, created_at, updated_at FROM bot_downloads WHERE status != 'organized' ORDER BY updated_at DESC"
            ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


@retry_on_locked()
def delete_download(file_id: str) -> None:
    """Elimina una entrada de descarga cuando ya fue organizada o cancelada."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute("DELETE FROM bot_downloads WHERE file_id = ?", (str(file_id),))
        conn.close()


@retry_on_locked()
def cleanup_stale_sessions(hours: int = 24) -> None:
    """Elimina sesiones inactivas por más de X horas."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "DELETE FROM bot_sessions WHERE updated_at < datetime('now', '-' || ? || ' hours')",
                (str(hours),),
            )
        conn.close()


def _bootstrap_from_legacy_files(conn: sqlite3.Connection) -> None:
    """Importa el estado legacy si alguna tabla sigue vacía."""
    try:
        tmp_dir = _get_db_path().parent
        completed_file = tmp_dir / "completed.txt"
        pending_file = tmp_dir / "pending-compression.txt"
        progress_file = tmp_dir / "progress.json"

        with conn:
            completed_count = conn.execute("SELECT COUNT(*) FROM completed_files").fetchone()[0]
            if completed_count == 0 and completed_file.exists():
                completed_rows = []
                with open(completed_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        path = line.strip()
                        if path:
                            completed_rows.append((path, None))
                if completed_rows:
                    conn.executemany(
                        "REPLACE INTO completed_files(path, label, completed_at) VALUES (?, ?, datetime('now'))",
                        completed_rows,
                    )

            pending_count = conn.execute("SELECT COUNT(*) FROM pending_files").fetchone()[0]
            if pending_count == 0 and pending_file.exists():
                pending_rows = []
                with open(pending_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        path = line.strip()
                        if path:
                            pending_rows.append((path, None))
                if pending_rows:
                    conn.executemany(
                        "REPLACE INTO pending_files(path, label, added_at) VALUES (?, ?, datetime('now'))",
                        pending_rows,
                    )

            progress_count = conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
            if progress_count == 0 and progress_file.exists():
                try:
                    progress_data = json.loads(progress_file.read_text(encoding="utf-8"))
                    conn.execute(
                        "REPLACE INTO progress(key, data, updated_at) VALUES ('main', ?, datetime('now'))",
                        (json.dumps(progress_data, ensure_ascii=False),),
                    )
                except Exception:
                    pass
    except Exception:
        pass


def _register_for_wrapper(path) -> Optional[MediaRecord]:
    """Registra la ruta sin exigir que esté asentada; None si no existe o no se puede leer."""
    rec, _event = register(path, require_settled=False)
    if rec is None:
        logger.warning("No se pudo registrar %s (no existe o no se puede leer)", path)
    return rec


def mark_completed(path, label: Optional[str] = None) -> None:
    """Marca un archivo como completado (por huella, sobrevive a renombrados)."""
    rec = _register_for_wrapper(path)
    if rec is not None:
        set_status(rec.fingerprint, "done")


def is_completed(path) -> bool:
    """Devuelve True si el archivo está en estado done o skipped."""
    rec = get_by_path(path)
    return rec is not None and rec.status in ("done", "skipped")


def add_pending(path, label: Optional[str] = None) -> None:
    """Registra un archivo y lo deja en pending.

    Si el archivo (o el contenido del que es renombrado o duplicado) ya está
    done/skipped/failed, conserva ese estado en cualquier evento para no
    reintroducir el reprocesado.
    """
    rec, _event = register(path, require_settled=False)
    if rec is None:
        logger.warning("No se pudo registrar %s (no existe o no se puede leer)", path)
        return
    if rec.status in ("done", "skipped", "failed"):
        return
    set_status(rec.fingerprint, "pending")


@retry_on_locked()
def remove_pending(path) -> None:
    """Saca una ruta de pendientes.

    Si no existe, la fila pasa a missing; si existe y estaba pending, pasa a
    skipped; en otros estados no cambia nada.
    """
    exists = Path(str(path)).exists()
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            if exists:
                conn.execute(
                    "UPDATE media_files SET status = 'skipped', updated_at = datetime('now') "
                    "WHERE path = ? AND status = 'pending'",
                    (str(path),),
                )
            else:
                conn.execute(
                    """
                    UPDATE media_files
                    SET status_before_missing = status, status = 'missing',
                        missing_since = datetime('now'), updated_at = datetime('now')
                    WHERE path = ? AND status != 'missing'
                    """,
                    (str(path),),
                )
        conn.close()


def get_pending_files() -> List[Path]:
    """Devuelve las rutas pendientes (por prioridad y luego created_at) como objetos Path."""
    return [Path(r.path) for r in list_by_status("pending")]


def get_all_completed_files() -> List[Path]:
    """Devuelve las rutas completadas u omitidas (done o skipped)."""
    return [Path(r.path) for r in list_by_status("done", "skipped")]


@retry_on_locked()
def save_progress(data: Dict) -> None:
    """Guarda un dict serializado en la fila 'main' de la tabla progress."""
    init_db()
    serialized = json.dumps(data, ensure_ascii=False)
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "REPLACE INTO progress(key, data, updated_at) VALUES ('main', ?, datetime('now'))",
                (serialized,),
            )
        conn.close()


@retry_on_locked()
def load_progress() -> Dict:
    """Carga y devuelve el dict de progreso; devuelve {} si no existe."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        cur = conn.execute("SELECT data FROM progress WHERE key = 'main' LIMIT 1")
        row = cur.fetchone()
        conn.close()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Estado de medios identificado por huella de contenido (esquema v2)
# ---------------------------------------------------------------------------

_LANG_MAP = {
    "ja": "jpn",
    "en": "eng",
    "es": "spa",
    "fr": "fre",
    "de": "ger",
    "it": "ita",
    "pt": "por",
}

_MEDIA_COLUMNS = "fingerprint, path, status, attempts, derived_from"


@dataclass
class MediaRecord:
    fingerprint: str
    path: str
    status: str
    attempts: int
    derived_from: Optional[str]


def _to_record(row: sqlite3.Row) -> MediaRecord:
    return MediaRecord(
        fingerprint=row["fingerprint"],
        path=row["path"],
        status=row["status"],
        attempts=int(row["attempts"] or 0),
        derived_from=row["derived_from"],
    )


@contextmanager
def _immediate(conn: sqlite3.Connection):
    """Transacción BEGIN IMMEDIATE: toma el bloqueo de escritura antes de leer."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _fetch_by(conn: sqlite3.Connection, column: str, value: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_MEDIA_COLUMNS}, status_before_missing FROM media_files WHERE {column} = ?",
        (value,),
    ).fetchone()


def _fetch_record(conn: sqlite3.Connection, fp: str) -> Optional[MediaRecord]:
    row = _fetch_by(conn, "fingerprint", fp)
    return _to_record(row) if row else None


def _delete_fp(conn: sqlite3.Connection, fp: str) -> None:
    """Borra una huella y todo lo que cuelga de ella."""
    conn.execute("DELETE FROM media_files WHERE fingerprint = ?", (fp,))
    conn.execute("DELETE FROM stream_languages WHERE fingerprint = ?", (fp,))
    conn.execute("DELETE FROM retag_failures WHERE fingerprint = ?", (fp,))
    conn.execute("DELETE FROM subtitle_queue WHERE fingerprint = ?", (fp,))
    conn.execute("DELETE FROM language_attempts WHERE fingerprint = ?", (fp,))
    conn.execute("DELETE FROM avisos WHERE fingerprint = ?", (fp,))


def _move_fp(conn: sqlite3.Connection, old_fp: str, new_fp: str) -> None:
    """Cambia la huella de una fila conservando idiomas y cola de subtítulos."""
    conn.execute(
        "UPDATE media_files SET fingerprint = ?, updated_at = datetime('now') WHERE fingerprint = ?",
        (new_fp, old_fp),
    )
    conn.execute("UPDATE OR REPLACE stream_languages SET fingerprint = ? WHERE fingerprint = ?", (new_fp, old_fp))
    conn.execute("UPDATE OR REPLACE subtitle_queue SET fingerprint = ? WHERE fingerprint = ?", (new_fp, old_fp))
    conn.execute("UPDATE OR REPLACE language_attempts SET fingerprint = ? WHERE fingerprint = ?", (new_fp, old_fp))
    conn.execute("UPDATE OR REPLACE avisos SET fingerprint = ? WHERE fingerprint = ?", (new_fp, old_fp))


def _clear_missing(conn: sqlite3.Connection, fp: str) -> None:
    """Si la fila estaba `missing`, recupera su estado previo."""
    conn.execute(
        """
        UPDATE media_files
        SET status = COALESCE(status_before_missing, 'pending'),
            missing_since = NULL,
            status_before_missing = NULL,
            updated_at = datetime('now')
        WHERE fingerprint = ? AND (status = 'missing' OR missing_since IS NOT NULL)
        """,
        (fp,),
    )


def _insert_pending(conn: sqlite3.Connection, fp: str, path_s: str, derived_from: Optional[str] = None) -> None:
    conn.execute(
        """
        INSERT INTO media_files(fingerprint, path, status, attempts, derived_from, created_at, updated_at)
        VALUES (?, ?, 'pending', 0, ?, datetime('now'), datetime('now'))
        """,
        (fp, path_s, derived_from),
    )


def _cached_fingerprint(path: Path, st) -> str:
    """Huella desde fingerprint_cache si tamaño y mtime coinciden; si no, la calcula."""
    with DB_LOCK:
        conn = _get_conn()
        row = conn.execute(
            "SELECT size, mtime_ns, fingerprint FROM fingerprint_cache WHERE path = ?",
            (str(path),),
        ).fetchone()
        conn.close()
    if row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
        return row["fingerprint"]
    return fingerprint(path)


def _store_cache(conn: sqlite3.Connection, path_s: str, st, fp: str) -> None:
    conn.execute(
        "REPLACE INTO fingerprint_cache(path, size, mtime_ns, fingerprint) VALUES (?, ?, ?, ?)",
        (path_s, st.st_size, st.st_mtime_ns, fp),
    )


def _register_locked(conn: sqlite3.Connection, path_s: str, fp: str) -> Tuple[Optional[MediaRecord], str]:
    by_fp = _fetch_by(conn, "fingerprint", fp)
    by_path = _fetch_by(conn, "path", path_s)

    if by_fp is not None:
        old_path = by_fp["path"]
        if old_path == path_s:
            _clear_missing(conn, fp)
            return _fetch_record(conn, fp), "unchanged"
        if Path(old_path).exists():
            logger.warning(
                f"⚠️ Contenido duplicado: {path_s} es igual a {old_path}; se mantiene el registro original"
            )
            return _to_record(by_fp), "duplicate"
        if by_path is not None:
            _delete_fp(conn, by_path["fingerprint"])
        conn.execute(
            "UPDATE media_files SET path = ?, updated_at = datetime('now') WHERE fingerprint = ?",
            (path_s, fp),
        )
        conn.execute("DELETE FROM fingerprint_cache WHERE path = ?", (old_path,))
        _clear_missing(conn, fp)
        logger.info(f"Renombrado: {Path(old_path).name} → {Path(path_s).name}")
        return _fetch_record(conn, fp), "renamed"

    if by_path is not None:
        if by_path["status"] == "processing":
            _move_fp(conn, by_path["fingerprint"], fp)
            return _fetch_record(conn, fp), "adopted"
        _delete_fp(conn, by_path["fingerprint"])
        _insert_pending(conn, fp, path_s)
        return _fetch_record(conn, fp), "replaced"

    _insert_pending(conn, fp, path_s)
    return _fetch_record(conn, fp), "new"


@retry_on_locked()
def register(path, require_settled: bool = True) -> Tuple[Optional[MediaRecord], str]:
    """Registra un video por su huella.

    Devuelve (registro, evento) con evento en "new", "renamed", "unchanged",
    "duplicate", "unsettled", "adopted" o "replaced".
    """
    init_db()
    p = Path(path)
    path_s = str(p)
    if require_settled and not is_settled(p):
        return None, "unsettled"
    try:
        st = p.stat()
        fp = _cached_fingerprint(p, st)
    except OSError:
        return None, "unsettled"

    with DB_LOCK:
        conn = _get_conn()
        try:
            for _ in range(5):
                try:
                    with _immediate(conn):
                        _store_cache(conn, path_s, st, fp)
                        return _register_locked(conn, path_s, fp)
                except sqlite3.IntegrityError:
                    # Otro proceso insertó la misma huella o ruta primero: se vuelve a leer.
                    continue
            raise sqlite3.IntegrityError(f"No se pudo registrar {path_s} tras varios intentos")
        finally:
            conn.close()


@retry_on_locked()
def get_by_path(path) -> Optional[MediaRecord]:
    """Devuelve el registro de una ruta, o None si no está registrada."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        row = _fetch_by(conn, "path", str(path))
        conn.close()
    return _to_record(row) if row else None


@retry_on_locked()
def get_by_fingerprint(fp: str) -> Optional[MediaRecord]:
    """Devuelve el registro de una huella, o None si no existe."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        record = _fetch_record(conn, fp)
        conn.close()
    return record


@retry_on_locked()
def list_by_status(*statuses: str) -> List[MediaRecord]:
    """Devuelve los registros con alguno de los estados indicados (al menos uno)."""
    if not statuses:
        raise ValueError("list_by_status necesita al menos un estado")
    init_db()
    marks = ", ".join("?" for _ in statuses)
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(
            f"SELECT {_MEDIA_COLUMNS} FROM media_files WHERE status IN ({marks}) ORDER BY prioridad DESC, created_at ASC, path ASC",
            statuses,
        ).fetchall()
        conn.close()
    return [_to_record(r) for r in rows]


@retry_on_locked()
def set_status(fp: str, status: str) -> None:
    """Fija el estado de una huella; "done" y "skipped" reinician el conteo de fallos."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            if status in ("done", "skipped"):
                conn.execute(
                    """
                    UPDATE media_files
                    SET status = ?, attempts = 0, first_failed_at = NULL, failure_type = NULL,
                        updated_at = datetime('now')
                    WHERE fingerprint = ?
                    """,
                    (status, fp),
                )
            else:
                conn.execute(
                    "UPDATE media_files SET status = ?, updated_at = datetime('now') WHERE fingerprint = ?",
                    (status, fp),
                )
        conn.close()


@retry_on_locked()
def reiniciar_fallos(fp: str) -> None:
    """Borra el conteo de fallos de una huella sin cambiar su estado (reproceso a mano)."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                """
                UPDATE media_files
                SET attempts = 0, first_failed_at = NULL, failure_type = NULL,
                    updated_at = datetime('now')
                WHERE fingerprint = ?
                """,
                (fp,),
            )
        conn.close()


@retry_on_locked()
def set_prioridad(fp: str, prioridad: int) -> None:
    """Fija la prioridad de una huella en la cola (mayor valor se procesa antes)."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "UPDATE media_files SET prioridad = ?, updated_at = datetime('now') WHERE fingerprint = ?",
                (int(prioridad), fp),
            )
        conn.close()


@retry_on_locked()
def record_failure(fp: str, error: str, resource: bool) -> str:
    """Anota un fallo y devuelve el estado resultante ("pending" o "failed").

    Los fallos de recursos no cuentan como intento. Un fallo de contenido pasa
    a "failed" al llegar a MAX_ATTEMPTS o tras FAILURE_WINDOW_HOURS desde el primero.
    Una fila ya en "failed" no cambia y devuelve "failed".
    Devuelve "" si la huella no está registrada.
    """
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            with _immediate(conn):
                current = _fetch_by(conn, "fingerprint", fp)
                if current is None:
                    logger.warning(f"⚠️ Fallo de una huella no registrada: {fp}")
                    return ""
                if current["status"] == "failed":
                    return "failed"
                if resource:
                    conn.execute(
                        """
                        UPDATE media_files
                        SET status = 'pending', failure_type = 'resource', last_error = ?,
                            updated_at = datetime('now')
                        WHERE fingerprint = ?
                        """,
                        (error, fp),
                    )
                    return "pending"
                conn.execute(
                    """
                    UPDATE media_files
                    SET attempts = COALESCE(attempts, 0) + 1,
                        first_failed_at = COALESCE(first_failed_at, datetime('now')),
                        failure_type = 'content', last_error = ?
                    WHERE fingerprint = ?
                    """,
                    (error, fp),
                )
                row = conn.execute(
                    "SELECT attempts, first_failed_at <= datetime('now', ?) AS expired FROM media_files WHERE fingerprint = ?",
                    (f"-{FAILURE_WINDOW_HOURS} hours", fp),
                ).fetchone()
                status = "failed" if row["attempts"] >= MAX_ATTEMPTS or row["expired"] else "pending"
                conn.execute(
                    "UPDATE media_files SET status = ?, updated_at = datetime('now') WHERE fingerprint = ?",
                    (status, fp),
                )
                if status == "failed":
                    logger.warning(f"⚠️ En cuarentena tras {row['attempts']} fallo(s): {fp}")
                return status
        finally:
            conn.close()


@retry_on_locked()
def record_rewrite(old_fp: str, new_path, carry_languages: bool = True) -> MediaRecord:
    """Pasa el estado de old_fp a la huella del archivo reescrito en new_path.

    Con carry_languages=False no se trasladan los idiomas por stream: tras una
    recompresión los índices de pista del original no valen para la salida
    (las etiquetas ya van escritas en el archivo).
    """
    init_db()
    p = Path(new_path)
    path_s = str(p)
    st = p.stat()
    new_fp = _cached_fingerprint(p, st)

    with DB_LOCK:
        conn = _get_conn()
        try:
            with _immediate(conn):
                _store_cache(conn, path_s, st, new_fp)
                old = _fetch_by(conn, "fingerprint", old_fp)
                if old is None:
                    logger.warning(f"⚠️ Reescritura de una huella no registrada ({old_fp}); se registra {path_s}")
                    by_path = _fetch_by(conn, "path", path_s)
                    if by_path is not None and by_path["fingerprint"] != new_fp:
                        _delete_fp(conn, by_path["fingerprint"])
                    if _fetch_by(conn, "fingerprint", new_fp) is None:
                        _insert_pending(conn, new_fp, path_s, derived_from=old_fp)
                    else:
                        conn.execute(
                            "UPDATE media_files SET path = ?, updated_at = datetime('now') WHERE fingerprint = ?",
                            (path_s, new_fp),
                        )
                    return _fetch_record(conn, new_fp)

                old_path = old["path"]
                if new_fp != old_fp:
                    if _fetch_by(conn, "fingerprint", new_fp) is not None:
                        _delete_fp(conn, new_fp)
                    if not carry_languages:
                        conn.execute("DELETE FROM stream_languages WHERE fingerprint = ?", (old_fp,))
                    _move_fp(conn, old_fp, new_fp)
                by_path = _fetch_by(conn, "path", path_s)
                if by_path is not None and by_path["fingerprint"] != new_fp:
                    _delete_fp(conn, by_path["fingerprint"])
                conn.execute(
                    """
                    UPDATE media_files
                    SET path = ?, derived_from = CASE WHEN ? THEN ? ELSE derived_from END,
                        updated_at = datetime('now')
                    WHERE fingerprint = ?
                    """,
                    (path_s, new_fp != old_fp, old_fp, new_fp),
                )
                _clear_missing(conn, new_fp)
                if old_path != path_s:
                    conn.execute("DELETE FROM fingerprint_cache WHERE path = ?", (old_path,))
                return _fetch_record(conn, new_fp)
        finally:
            conn.close()


@retry_on_locked()
def recover_processing() -> int:
    """Devuelve a "pending" lo que quedó en "processing" (sin sumar intento)."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            cur = conn.execute(
                "UPDATE media_files SET status = 'pending', updated_at = datetime('now') WHERE status = 'processing'"
            )
            count = cur.rowcount
        conn.close()
    if count:
        logger.info(f"🔄 Recuperados {count} archivo(s) que quedaron en proceso")
    return count


@retry_on_locked()
def mark_missing_except(existing_paths: Set[str], roots: Optional[List[str]] = None) -> int:
    """Marca como "missing" las filas cuya ruta no está en existing_paths.

    Si se indican `roots`, solo se consideran las filas bajo alguna de esas
    raíces, para no marcar como ausente lo que está en un montaje caído o vacío.
    """
    init_db()
    existing = {str(p) for p in existing_paths}
    prefixes = None if roots is None else tuple(str(r).rstrip("/") + "/" for r in roots)
    with DB_LOCK:
        conn = _get_conn()
        try:
            with _immediate(conn):
                rows = conn.execute(
                    "SELECT fingerprint, path FROM media_files WHERE status != 'missing'"
                ).fetchall()
                gone = [
                    (r["fingerprint"],)
                    for r in rows
                    if r["path"] not in existing and (prefixes is None or r["path"].startswith(prefixes))
                ]
                conn.executemany(
                    """
                    UPDATE media_files
                    SET status_before_missing = status, status = 'missing',
                        missing_since = datetime('now'), updated_at = datetime('now')
                    WHERE fingerprint = ?
                    """,
                    gone,
                )
        finally:
            conn.close()
    return len(gone)


@retry_on_locked()
def purge_missing(days: int = 30) -> int:
    """Borra las filas "missing" desde hace al menos `days` días."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            with _immediate(conn):
                rows = conn.execute(
                    """
                    SELECT fingerprint, path FROM media_files
                    WHERE status = 'missing' AND missing_since <= datetime('now', ?)
                    """,
                    (f"-{int(days)} days",),
                ).fetchall()
                for r in rows:
                    _delete_fp(conn, r["fingerprint"])
                    conn.execute("DELETE FROM fingerprint_cache WHERE path = ?", (r["path"],))
        finally:
            conn.close()
    if rows:
        logger.info(f"🗑️ Purgados {len(rows)} registro(s) ausentes desde hace {days} días o más")
    return len(rows)


def normalize_lang(code: str) -> str:
    """Normaliza un código de idioma a ISO 639-2 (3 letras); vacío → "und"."""
    value = (code or "").strip().lower()
    if not value:
        return "und"
    return _LANG_MAP.get(value, value)


@retry_on_locked()
def set_stream_language(fp: str, stream_index: int, lang: str, source: str) -> None:
    """Guarda el idioma de un stream de audio (normalizado)."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "REPLACE INTO stream_languages(fingerprint, stream_index, lang, source) VALUES (?, ?, ?, ?)",
                (fp, int(stream_index), normalize_lang(lang), source),
            )
        conn.close()


@retry_on_locked()
def get_stream_languages(fp: str) -> Dict[int, str]:
    """Devuelve {índice_de_stream: idioma} de una huella."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT stream_index, lang FROM stream_languages WHERE fingerprint = ? ORDER BY stream_index",
            (fp,),
        ).fetchall()
        conn.close()
    return {int(r["stream_index"]): r["lang"] for r in rows}


@retry_on_locked()
def borrar_idiomas_detectados(fp: str) -> int:
    """Borra los idiomas que Whisper detectó para una huella (fuente "whisper").

    Conserva los corregidos a mano y los de otras fuentes. Devuelve cuántos borró.
    """
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            n = conn.execute(
                "DELETE FROM stream_languages WHERE fingerprint = ? AND source = 'whisper'", (fp,)
            ).rowcount
        conn.close()
    return n


@retry_on_locked()
def record_retag_failure(fp: str, error: str) -> None:
    """Anota que el re-etiquetado de esta huella falló, para no repetir el remux."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "REPLACE INTO retag_failures(fingerprint, failed_at, error) VALUES (?, datetime('now'), ?)",
                (fp, (error or "")[:500]),
            )
        conn.close()


@retry_on_locked()
def has_retag_failure(fp: str) -> bool:
    """True si el re-etiquetado de esta huella ya falló antes."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        row = conn.execute("SELECT 1 FROM retag_failures WHERE fingerprint = ?", (fp,)).fetchone()
        conn.close()
    return row is not None


@retry_on_locked()
def record_language_attempt(fp: str) -> None:
    """Anota que Whisper no pudo detectar algún idioma de esta huella terminada."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "REPLACE INTO language_attempts(fingerprint, attempted_at) VALUES (?, datetime('now'))",
                (fp,),
            )
        conn.close()


@retry_on_locked()
def marcar_aviso(fp: str, tipo: str, detalle: str = "") -> bool:
    """Marca un aviso para la huella (o actualiza su detalle).

    Args:
        fp: Huella del archivo.
        tipo: Uno de TIPOS_AVISO.
        detalle: Texto que muestran el panel y el aviso.

    Returns:
        True si el aviso no existía: quien notifica lo hace solo esa vez.

    Raises:
        ValueError: Si el tipo no está en TIPOS_AVISO.
    """
    if tipo not in TIPOS_AVISO:
        raise ValueError(f"Tipo de aviso desconocido: {tipo}")
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            with conn:
                existia = conn.execute(
                    "SELECT 1 FROM avisos WHERE fingerprint = ? AND tipo = ?", (fp, tipo)
                ).fetchone()
                if existia:
                    conn.execute(
                        "UPDATE avisos SET detalle = ? WHERE fingerprint = ? AND tipo = ?", (detalle, fp, tipo)
                    )
                else:
                    conn.execute(
                        "INSERT INTO avisos(fingerprint, tipo, detalle, creado) VALUES (?, ?, ?, datetime('now'))",
                        (fp, tipo, detalle),
                    )
        finally:
            conn.close()
    return existia is None


@retry_on_locked()
def quitar_aviso(fp: str, tipo: str) -> None:
    """Quita un aviso de la huella; si no existe, no hace nada."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            with conn:
                conn.execute("DELETE FROM avisos WHERE fingerprint = ? AND tipo = ?", (fp, tipo))
        finally:
            conn.close()


@retry_on_locked()
def listar_avisos(tipo: Optional[str] = None) -> List[Tuple[str, str, str, str, Optional[str]]]:
    """Avisos marcados, los más recientes primero.

    Args:
        tipo: Si se da, solo los de ese tipo.

    Returns:
        Tuplas (huella, tipo, detalle, creado, ruta del archivo o None).
    """
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            rows = conn.execute(
                """
                SELECT a.fingerprint, a.tipo, COALESCE(a.detalle, ''), a.creado, m.path
                FROM avisos a LEFT JOIN media_files m ON m.fingerprint = a.fingerprint
                WHERE ? IS NULL OR a.tipo = ?
                ORDER BY a.creado DESC, a.fingerprint
                """,
                (tipo, tipo),
            ).fetchall()
        finally:
            conn.close()
    return [tuple(r) for r in rows]


@retry_on_locked()
def list_language_candidates(retry_days: int = LANGUAGE_RETRY_DAYS) -> List[MediaRecord]:
    """Registros done/skipped que el detector de idiomas puede revisar.

    Excluye los que ya fallaron al re-etiquetar y los que pasaron por Whisper
    sin resultado hace menos de retry_days días.
    """
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(
            f"""
            SELECT {_MEDIA_COLUMNS} FROM media_files m
            WHERE status IN ('done', 'skipped')
              AND NOT EXISTS (SELECT 1 FROM retag_failures r WHERE r.fingerprint = m.fingerprint)
              AND NOT EXISTS (
                  SELECT 1 FROM language_attempts a
                  WHERE a.fingerprint = m.fingerprint AND a.attempted_at > datetime('now', ?)
              )
            ORDER BY created_at ASC, path ASC
            """,
            (f"-{int(retry_days)} days",),
        ).fetchall()
        conn.close()
    return [_to_record(r) for r in rows]


@retry_on_locked()
def get_meta(key: str) -> Optional[str]:
    """Valor de la tabla meta, o None si no existe."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        conn.close()
    return row["value"] if row else None


@retry_on_locked()
def mark_legacy_migrated() -> None:
    """Deja constancia (con la fecha UTC) de que el estado anterior ya se migró."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute("REPLACE INTO meta(key, value) VALUES ('legacy_migrated', datetime('now'))")
        conn.close()


@retry_on_locked()
def migration_pending() -> bool:
    """True si hay historial anterior (completed_files) sin migrar al estado por huella.

    Sin BD o con completed_files vacía (instalación nueva) no hay nada que migrar.
    """
    if not _get_db_path().exists():
        return False
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            if conn.execute("SELECT 1 FROM completed_files LIMIT 1").fetchone() is None:
                return False
            return conn.execute("SELECT 1 FROM meta WHERE key = 'legacy_migrated'").fetchone() is None
        finally:
            conn.close()


@retry_on_locked()
def replace_subtitle_queue(paths: List[str]) -> None:
    """Reemplaza la cola de subtítulos; conserva la fecha de los que ya estaban.

    Solo encola rutas ya registradas (el escáner las registra antes de armar la
    cola); las desconocidas, p. ej. archivos aún en copia, se omiten.
    """
    init_db()
    fps: List[str] = []
    for path in paths:
        rec = get_by_path(path)
        if rec is None:
            logger.debug(f"Se omite de la cola de subtítulos (no registrado): {path}")
            continue
        if rec.fingerprint not in fps:
            fps.append(rec.fingerprint)

    with DB_LOCK:
        conn = _get_conn()
        try:
            with _immediate(conn):
                current = {r["fingerprint"] for r in conn.execute("SELECT fingerprint FROM subtitle_queue")}
                keep = set(fps)
                conn.executemany(
                    "DELETE FROM subtitle_queue WHERE fingerprint = ?",
                    [(fp,) for fp in current - keep],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO subtitle_queue(fingerprint, added_at) VALUES (?, datetime('now'))",
                    [(fp,) for fp in fps],
                )
        finally:
            conn.close()


@retry_on_locked()
def get_subtitle_queue() -> List[Path]:
    """Devuelve las rutas actuales de la cola de subtítulos (sin las ausentes)."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT m.path FROM subtitle_queue q
            JOIN media_files m ON m.fingerprint = q.fingerprint
            WHERE m.status != 'missing'
            ORDER BY q.added_at ASC, q.rowid ASC
            """
        ).fetchall()
        conn.close()
    return [Path(r["path"]) for r in rows]


@retry_on_locked()
def remove_from_subtitle_queue(paths: List[str]) -> None:
    """Quita rutas de la cola de subtítulos."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.executemany(
                "DELETE FROM subtitle_queue WHERE fingerprint IN (SELECT fingerprint FROM media_files WHERE path = ?)",
                [(str(p),) for p in paths],
            )
        conn.close()


# --- Tareas del panel --------------------------------------------------------
# El panel encola acciones pesadas; mediajelly_tareas.py (contenedor del cron)
# las ejecuta. Estados: pendiente, corriendo, ok, error.

TIPOS_TAREA = {"ciclo", "retag", "detectar"}
_TAREA_COLUMNAS = "id, tipo, argumentos, estado, salida, creada, iniciada, terminada"


def _tarea_a_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["argumentos"] = json.loads(d["argumentos"])
    return d


@retry_on_locked()
def crear_tarea(tipo: str, argumentos: dict) -> Tuple[int, bool]:
    """Encola una tarea. Devuelve (id, creada); si ya hay una pendiente o corriendo
    igual (mismo tipo y argumentos), devuelve la existente con creada=False."""
    if tipo not in TIPOS_TAREA:
        raise ValueError(f"Tipo de tarea desconocido: {tipo!r}")
    args_json = json.dumps(argumentos, sort_keys=True, ensure_ascii=False)
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            conn.isolation_level = None
            with _immediate(conn):
                row = conn.execute(
                    "SELECT id FROM panel_tareas WHERE tipo = ? AND argumentos = ? "
                    "AND estado IN ('pendiente', 'corriendo') ORDER BY id LIMIT 1",
                    (tipo, args_json),
                ).fetchone()
                if row:
                    return int(row["id"]), False
                cur = conn.execute(
                    "INSERT INTO panel_tareas(tipo, argumentos, estado, creada) "
                    "VALUES (?, ?, 'pendiente', datetime('now'))",
                    (tipo, args_json),
                )
                return int(cur.lastrowid), True
        finally:
            conn.close()


@retry_on_locked()
def hay_tareas_pendientes() -> bool:
    """Consulta barata: ¿queda alguna tarea pendiente? (el ejecutor la usa cada minuto)."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        row = conn.execute("SELECT 1 FROM panel_tareas WHERE estado = 'pendiente' LIMIT 1").fetchone()
        conn.close()
    return row is not None


@retry_on_locked()
def tomar_siguiente_tarea() -> Optional[dict]:
    """Pasa de forma atómica la tarea pendiente más antigua a corriendo y la devuelve."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            conn.isolation_level = None
            with _immediate(conn):
                row = conn.execute(
                    f"SELECT {_TAREA_COLUMNAS} FROM panel_tareas WHERE estado = 'pendiente' "
                    "ORDER BY id ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE panel_tareas SET estado = 'corriendo', iniciada = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                row = conn.execute(
                    f"SELECT {_TAREA_COLUMNAS} FROM panel_tareas WHERE id = ?", (row["id"],)
                ).fetchone()
                return _tarea_a_dict(row)
        finally:
            conn.close()


@retry_on_locked()
def terminar_tarea(id: int, ok: bool, salida: str) -> None:
    """Cierra una tarea como ok o error con su salida."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            conn.execute(
                "UPDATE panel_tareas SET estado = ?, salida = ?, terminada = datetime('now') WHERE id = ?",
                ("ok" if ok else "error", salida, id),
            )
        conn.close()


@retry_on_locked()
def liberar_tareas_colgadas(minutos: int = 120) -> int:
    """Marca como error las tareas corriendo desde hace más de `minutos`. Devuelve cuántas."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        with conn:
            cur = conn.execute(
                "UPDATE panel_tareas SET estado = 'error', salida = ?, terminada = datetime('now') "
                "WHERE estado = 'corriendo' AND iniciada < datetime('now', ?)",
                ("Interrumpida (proceso detenido)", f"-{int(minutos)} minutes"),
            )
            n = cur.rowcount
        conn.close()
    return n


@retry_on_locked()
def reasignar_tareas(fp_viejo: str, fp_nuevo: str) -> int:
    """Pasa a fp_nuevo las tareas pendientes que apuntan a fp_viejo (tras reescribir
    el archivo, p. ej. un retag). Devuelve cuántas cambió."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        try:
            conn.isolation_level = None
            with _immediate(conn):
                cambiadas = 0
                for row in conn.execute(
                    "SELECT id, argumentos FROM panel_tareas WHERE estado = 'pendiente'"
                ).fetchall():
                    argumentos = json.loads(row["argumentos"])
                    if not isinstance(argumentos, dict) or argumentos.get("fp") != fp_viejo:
                        continue
                    argumentos["fp"] = fp_nuevo
                    # mismo formato que crear_tarea, para que la deduplicación siga valiendo
                    conn.execute(
                        "UPDATE panel_tareas SET argumentos = ? WHERE id = ?",
                        (json.dumps(argumentos, sort_keys=True, ensure_ascii=False), row["id"]),
                    )
                    cambiadas += 1
                return cambiadas
        finally:
            conn.close()


@retry_on_locked()
def listar_tareas(limite: int = 20) -> List[dict]:
    """Devuelve las tareas más recientes primero."""
    init_db()
    with DB_LOCK:
        conn = _get_conn()
        rows = conn.execute(
            f"SELECT {_TAREA_COLUMNAS} FROM panel_tareas ORDER BY id DESC LIMIT ?", (int(limite),)
        ).fetchall()
        conn.close()
    return [_tarea_a_dict(r) for r in rows]

import os
import httpx
import asyncio
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from dotenv import load_dotenv

load_dotenv()

INSFORGE_BASE_URL = os.getenv("INSFORGE_BASE_URL", "")
INSFORGE_SERVICE_KEY = os.getenv("INSFORGE_SERVICE_KEY", "")
ENABLE_LOCAL_DB_FALLBACK = os.getenv("ENABLE_LOCAL_DB_FALLBACK", "true").lower() != "false"
LOCAL_DB_PATH = Path(os.getenv("LOCAL_DB_PATH", Path(__file__).resolve().parents[1] / "data" / "local_db.json"))
REMOTE_RETRY_AFTER_SECONDS = int(os.getenv("INSFORGE_RETRY_AFTER_SECONDS", "60"))
_remote_disabled_until = 0.0


def _is_remote_unavailable(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {502, 503, 504}

    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
        return True

    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "503",
            "502",
            "504",
            "service temporarily unavailable",
            "no backend services available",
            "all connection attempts failed",
            "connection refused",
            "timeout",
            "readtimeout",
            "connecterror",
        )
    )


def _load_local_db() -> dict:
    LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOCAL_DB_PATH.exists():
        return {}

    try:
        with LOCAL_DB_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_local_db(data: dict) -> None:
    LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = LOCAL_DB_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp_path.replace(LOCAL_DB_PATH)


def _local_rows(table: str) -> list:
    data = _load_local_db()
    rows = data.get(table, [])
    return rows if isinstance(rows, list) else []


def _matches_filters(row: dict, filters: dict | None) -> bool:
    if not filters:
        return True

    for key, expected in filters.items():
        actual = row.get(key)
        if str(actual).lower() != str(expected).lower():
            return False
    return True


def _apply_order(rows: list, order: str | None) -> list:
    if not order:
        return rows

    field, _, direction = order.partition(".")
    reverse = direction.lower() == "desc"
    return sorted(rows, key=lambda row: row.get(field) or "", reverse=reverse)


def _local_insert(table: str, payload: dict) -> dict:
    data = _load_local_db()
    rows = data.setdefault(table, [])
    now = datetime.now(timezone.utc).isoformat()
    record = deepcopy(payload)
    record.setdefault("id", str(uuid.uuid4()))
    record.setdefault("created_at", now)
    record.setdefault("updated_at", now)
    rows.append(record)
    _save_local_db(data)
    return record


def _local_select(table: str, filters: dict = None, order: str = None, limit: int = None) -> list:
    rows = [deepcopy(row) for row in _local_rows(table) if _matches_filters(row, filters)]
    rows = _apply_order(rows, order)
    return rows[:limit] if limit else rows


def _local_update(table: str, row_id: str, payload: dict) -> dict:
    data = _load_local_db()
    rows = data.setdefault(table, [])
    for row in rows:
        if str(row.get("id")) == str(row_id):
            row.update(deepcopy(payload))
            row.setdefault("id", str(row_id))
            row["updated_at"] = payload.get("updated_at", datetime.now(timezone.utc).isoformat())
            _save_local_db(data)
            return deepcopy(row)

    raise KeyError(f"Local row not found in {table}: {row_id}")


def _local_delete(table: str, row_id: str) -> None:
    data = _load_local_db()
    rows = data.setdefault(table, [])
    data[table] = [row for row in rows if str(row.get("id")) != str(row_id)]
    _save_local_db(data)


def _fallback_or_raise(error: Exception, action: str, table: str):
    global _remote_disabled_until
    if ENABLE_LOCAL_DB_FALLBACK and _is_remote_unavailable(error):
        _remote_disabled_until = monotonic() + REMOTE_RETRY_AFTER_SECONDS
        print(f"InsForge unavailable during {action} on {table}; using local JSON fallback at {LOCAL_DB_PATH}")
        return
    raise error


def _should_use_local_now() -> bool:
    return ENABLE_LOCAL_DB_FALLBACK and monotonic() < _remote_disabled_until


def _headers():
    return {
        "apikey": INSFORGE_SERVICE_KEY,
        "Authorization": f"Bearer {INSFORGE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def db_insert(table: str, payload: dict) -> dict:
    """Insert a row into an InsForge (PostgREST) table and return the created record."""
    if not INSFORGE_BASE_URL or not INSFORGE_SERVICE_KEY or _should_use_local_now():
        return _local_insert(table, payload)

    url = f"{INSFORGE_BASE_URL}/api/database/records/{table}"
    # InsForge requires the payload to be an array even for single inserts
    payload_list = [payload]
    
    # We must add Prefer: return=representation if not already present
    headers = _headers()
    headers["Prefer"] = "return=representation"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload_list, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data[0] if isinstance(data, list) and len(data) > 0 else data
    except Exception as e:
        _fallback_or_raise(e, "insert", table)
        return _local_insert(table, payload)


async def db_select(table: str, filters: dict = None, order: str = None, limit: int = None, select: str = "*", retries: int = 0) -> list:
    """Select rows from an InsForge (PostgREST) table with retry logic."""
    if not INSFORGE_BASE_URL or not INSFORGE_SERVICE_KEY or _should_use_local_now():
        return _local_select(table, filters=filters, order=order, limit=limit)

    url = f"{INSFORGE_BASE_URL}/api/database/records/{table}"
    params = {"select": select}
    if filters:
        params.update({f"{k}": f"eq.{v}" for k, v in filters.items()})
    if order:
        params["order"] = order
    if limit:
        params["limit"] = str(limit)

    last_exception = None
    
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params, headers=_headers())
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            last_exception = e
            if e.response.status_code == 503 and attempt < retries:
                # Wait before retrying (shorter wait time)
                wait_time = 1
                print(f"InsForge 503 error, retrying in {wait_time} seconds... (attempt {attempt + 1}/{retries + 1})")
                await asyncio.sleep(wait_time)
                continue
            _fallback_or_raise(e, "select", table)
            return _local_select(table, filters=filters, order=order, limit=limit)
        except Exception as e:
            last_exception = e
            if attempt < retries:
                wait_time = 1
                print(f"InsForge connection error, retrying in {wait_time} seconds... (attempt {attempt + 1}/{retries + 1})")
                await asyncio.sleep(wait_time)
                continue
            _fallback_or_raise(e, "select", table)
            return _local_select(table, filters=filters, order=order, limit=limit)
    
    # If we get here, all retries failed
    raise last_exception


async def db_select_single(table: str, filters: dict = None, select: str = "*", order: str = None) -> dict | None:
    """Select a single row. Returns None if not found."""
    rows = await db_select(table, filters=filters, order=order, limit=1, select=select)
    return rows[0] if rows else None


async def db_update(table: str, row_id: str, payload: dict) -> dict:
    """Update a row by id in an InsForge (PostgREST) table."""
    if not INSFORGE_BASE_URL or not INSFORGE_SERVICE_KEY or _should_use_local_now():
        return _local_update(table, row_id, payload)

    url = f"{INSFORGE_BASE_URL}/api/database/records/{table}"
    params = {"id": f"eq.{row_id}"}
    
    headers = _headers()
    headers["Prefer"] = "return=representation"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, json=payload, params=params, headers=headers)
            if not resp.is_success:
                raise httpx.HTTPStatusError(
                    f"Database update failed with {resp.status_code}: {resp.text}",
                    request=resp.request,
                    response=resp,
                )
            data = resp.json()
            # the response should be an array according to the docs
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            return data
    except Exception as e:
        _fallback_or_raise(e, "update", table)
        return _local_update(table, row_id, payload)


async def db_delete(table: str, row_id: str) -> None:
    """Delete a row by id from an InsForge (PostgREST) table."""
    if not INSFORGE_BASE_URL or not INSFORGE_SERVICE_KEY or _should_use_local_now():
        _local_delete(table, row_id)
        return

    url = f"{INSFORGE_BASE_URL}/api/database/records/{table}"
    params = {"id": f"eq.{row_id}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(url, params=params, headers=_headers())
            resp.raise_for_status()
    except Exception as e:
        _fallback_or_raise(e, "delete", table)
        _local_delete(table, row_id)

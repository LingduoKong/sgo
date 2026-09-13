"""
SGO Web Interface — FastAPI backend wrapping the SGO pipeline.

Provides a browser UI for:
  1. Describing an entity to evaluate
  2. Generating an evaluator cohort (LLM-generated or uploaded)
  3. Running evaluation against the cohort
  4. Running counterfactual probes to get the semantic gradient

Usage:
    uv run python web/app.py
    # Opens at http://localhost:8000
"""

import json
import os
import re
import asyncio
import time
import uuid
import concurrent.futures
import shutil
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI
import httpx
from contextvars import ContextVar
from web.llm_safety import LimitedClient
_model_auth = ContextVar("model_auth", default=None)

# Import core functions from existing scripts
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from evaluate import evaluate_one, analyze as analyze_eval, SYSTEM_PROMPT, BIAS_CALIBRATION_ADDENDUM
from counterfactual import probe_one, analyze_gradient, build_changes_block, compute_goal_weights
from ctr_calibrate import sigmoid, fit_platt_scaling, predict_ctr, ctr_derivative
from generate_cohort import generate_segment
from bias_audit import (
    reframe_entity, add_authority_signals, reorder_entity,
    run_paired_evaluation, analyze_probe, generate_report, HUMAN_BASELINES,
)
# Lazy imports — persona_loader pulls in HuggingFace datasets (~5s load)
_persona_loader = None
_stratified_sampler = None

def _lazy_persona_loader():
    global _persona_loader
    if _persona_loader is None:
        import persona_loader as _pl
        _persona_loader = _pl
    return _persona_loader

def _lazy_stratified_sampler():
    global _stratified_sampler
    if _stratified_sampler is None:
        import stratified_sampler as _ss
        _stratified_sampler = _ss
    return _stratified_sampler

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sgo")

app = FastAPI(title="SGO — Semantic Gradient Optimization", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# In-memory store for active sessions
sessions: dict = {}

# Authentication is always enabled; missing configuration fails closed.
from web.security import SecurityMiddleware
from web.auth import AuthError
app.add_middleware(SecurityMiddleware, sessions=sessions, model_context=_model_auth)
SESSION_MAX_AGE_HOURS = 24

# Nemotron datasets are identified and cached independently.  The old global
# cache made it possible for a newly selected country to accidentally reuse the
# USA table, so cache entries are keyed by the validated dataset id and path.
_nemotron_cache: dict[tuple[str, str], object] = {}
_nemotron_loading: dict[tuple[str, str], threading.Event] = {}
_nemotron_cache_lock = threading.Lock()
_setup_locks: dict[str, threading.Lock] = {}
_setup_locks_guard = threading.Lock()

NEMOTRON_SEARCH_PATHS = [
    Path("/data/nemotron"),  # HF Spaces persistent storage
    PROJECT_ROOT / "data" / "nemotron",
    Path.home() / "data" / "nvidia" / "Nemotron-Personas-USA",
    Path.home() / "data" / "nemotron",
    Path(os.getenv("NEMOTRON_DATA_DIR", "/nonexistent")),
]

DATASET_ROOT = Path("/data") if os.getenv("SPACE_ID") else PROJECT_ROOT / "data"
_dataset_paths: dict[str, Path] = {}


class DatasetIdentityError(ValueError):
    """A folder contains a different Nemotron country than requested."""


def dataset_path(dataset: str) -> Path:
    """Return the stable on-disk location for a known dataset id.

    USA keeps the original ``data/nemotron`` location for backwards
    compatibility. Every other country gets its own sibling directory.
    """
    if dataset not in NEMOTRON_DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    if dataset in _dataset_paths:
        return _dataset_paths[dataset]
    if dataset == "USA":
        legacy = DATASET_ROOT / "nemotron"
        if legacy.exists() or not (DATASET_ROOT / "nemotron-USA").exists():
            return legacy
    return DATASET_ROOT / f"nemotron-{dataset}"


def _dataset_info(path: Path) -> dict:
    try:
        with (path / "dataset_info.json").open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _identity_tokens(info: dict) -> str:
    values = [
        info.get("dataset_name", ""), info.get("description", ""),
        info.get("homepage", ""), info.get("config_name", ""),
    ]
    splits = info.get("splits", {})
    if isinstance(splits, dict):
        values.extend(str(v.get("dataset_name", "")) for v in splits.values() if isinstance(v, dict))
    return " ".join(values).lower()


def validate_dataset_identity(path: Path, dataset: str) -> dict:
    """Validate metadata before a folder can be used or overwritten."""
    if dataset not in NEMOTRON_DATASETS:
        raise DatasetIdentityError(f"Unknown dataset '{dataset}'. Choose a listed country.")
    info = _dataset_info(path)
    if not info:
        raise DatasetIdentityError(f"No readable dataset_info.json found at {path}.")
    expected = NEMOTRON_DATASETS[dataset].lower()
    expected_country = dataset.lower()
    primary = [str(info.get("dataset_name", "")).lower()]
    splits = info.get("splits", {})
    if isinstance(splits, dict):
        primary.extend(str(v.get("dataset_name", "")).lower() for v in splits.values() if isinstance(v, dict))
    primary = [value for value in primary if value]
    identity = " ".join(primary) if primary else _identity_tokens(info)
    if expected not in identity and f"nemotron-personas-{expected_country}" not in identity:
        found = info.get("dataset_name") or info.get("description") or "unknown dataset"
        raise DatasetIdentityError(
            f"This folder contains {found}, not {expected}. Choose a separate folder for {dataset}."
        )
    if dataset == "India":
        state_path = path / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            found_split = state.get("_split") if isinstance(state, dict) else None
            expected_split = dataset_split_name(dataset)
            if found_split != expected_split:
                raise DatasetIdentityError(
                    f"This India folder contains split {found_split or 'unknown'}, "
                    f"but English requires {expected_split}. Choose a separate English dataset folder."
                )
    return info


def _candidate_dataset_path(dataset: str) -> Path | None:
    candidates = [dataset_path(dataset)]
    if dataset == "USA":
        candidates.extend(NEMOTRON_SEARCH_PATHS)
    else:
        candidates.extend([p.parent / f"nemotron-{dataset}" for p in NEMOTRON_SEARCH_PATHS])
    seen = set()
    for path in candidates:
        path = Path(path)
        if str(path) in seen:
            continue
        seen.add(str(path))
        if (path / "dataset_info.json").exists():
            try:
                validate_dataset_identity(path, dataset)
            except DatasetIdentityError:
                log.warning("Ignoring mismatched %s dataset folder: %s", dataset, path)
                continue
            if _dataset_is_complete(path):
                return path
    return None


def _dataset_is_complete(path: Path) -> bool:
    """Require state metadata whose every referenced shard exists."""
    state_path = path / "state.json"
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    files = state.get("_data_files") if isinstance(state, dict) else None
    if not isinstance(files, list) or not files:
        return False
    names = [item.get("filename") for item in files if isinstance(item, dict)]
    return bool(names) and all(isinstance(name, str) and (path / name).is_file() for name in names)


def find_nemotron_path(dataset: str = "USA"):
    """Find a validated dataset path on disk. Returns path or None."""
    return _candidate_dataset_path(dataset)


def get_nemotron(dataset: str = "USA", data_dir=None):
    """Load one validated dataset, caching by country and absolute path."""
    path = Path(data_dir).expanduser().resolve() if data_dir else find_nemotron_path(dataset)
    if path is None:
        return None
    validate_dataset_identity(path, dataset)
    key = (dataset, str(path))
    with _nemotron_cache_lock:
        cached = _nemotron_cache.get(key)
        if cached is not None:
            return cached
        loading = _nemotron_loading.get(key)
        if loading is None:
            loading = threading.Event()
            _nemotron_loading[key] = loading
            owner = True
        else:
            owner = False
    if not owner:
        loading.wait()
        with _nemotron_cache_lock:
            return _nemotron_cache.get(key)
    try:
        ds = _lazy_persona_loader().load_personas(data_dir=path)
        with _nemotron_cache_lock:
            _nemotron_cache[key] = ds
        log.info("Nemotron %s loaded: %s personas from %s", dataset, len(ds), path)
        return ds
    except Exception as e:
        log.warning("Failed to load Nemotron %s from %s: %s", dataset, path, e)
        return None
    finally:
        with _nemotron_cache_lock:
            event = _nemotron_loading.pop(key, None)
            if event is not None:
                event.set()


def _setup_lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _setup_locks_guard:
        return _setup_locks.setdefault(key, threading.Lock())


# LLM client — server-managed configuration only.

def get_client(api_key=None, base_url=None):
    key = api_key or os.getenv("LLM_API_KEY")
    base = base_url or os.getenv("LLM_BASE_URL")
    if not key:
        raise HTTPException(503, "Model service is not configured. Contact the administrator.")
    # Browser clients cannot change the destination; do not follow redirects.
    client = OpenAI(api_key=key, base_url=base, timeout=45, max_retries=0,
                    http_client=httpx.Client(follow_redirects=False, timeout=45, trust_env=False))
    context = _model_auth.get()
    if context:
        auth, token = context
        def charge():
            user = auth.user(token)
            if not user:
                raise AuthError("Login expired or revoked", 401)
            auth.model_call(user['email'])
        return LimitedClient(client, charge)
    return client


def get_model(model=None):
    return model or os.getenv("LLM_MODEL_NAME", "openai/gpt-oss-120b")


def get_fast_model():
    """Smaller model for cheap setup calls (infer-spec, segments, filters, changes)."""
    return os.getenv("LLM_FAST_MODEL", "Qwen/Qwen2.5-7B-Instruct")


IS_SPACES = bool(os.getenv("SPACE_ID"))


def _llm_from_params(api_key: str = "", base_url: str = "", model: str = ""):
    """Extract LLM config from params. Falls back to env vars. Never stored."""
    return (
        get_client(api_key=api_key or None, base_url=base_url or None),
        get_model(model=model or None),
    )


def llm_from_request(request: Request):
    """Get LLM client+model from the current request. Never logs credentials."""
    return _llm_from_params(
        request.state.api_key, request.state.base_url, request.state.model
    )

NEMOTRON_DATASETS = {
    "USA": "nvidia/Nemotron-Personas-USA",
    "Japan": "nvidia/Nemotron-Personas-Japan",
    "India": "nvidia/Nemotron-Personas-India",
    "Singapore": "nvidia/Nemotron-Personas-Singapore",
    "Brazil": "nvidia/Nemotron-Personas-Brazil",
    "France": "nvidia/Nemotron-Personas-France",
}
# India publishes multiple language splits; English is the least
# surprising default for this English UI and keeps the selected identity clear.
NEMOTRON_CONFIGS = {"India": "default"}
NEMOTRON_LABELS = {"India": "India · English"}
NEMOTRON_SPLITS = {"India": "en_IN"}


def dataset_split_name(dataset: str) -> str:
    return NEMOTRON_SPLITS.get(dataset, "train")


def dataset_split_info(info: dict, dataset: str) -> dict:
    splits = info.get("splits", {}) if isinstance(info, dict) else {}
    if not isinstance(splits, dict):
        return {}
    return splits.get(dataset_split_name(dataset), {})


# ── Models ────────────────────────────────────────────────────────────────

class EntityInput(BaseModel):
    entity_text: str
    dataset: str | None = None


def dataset_metadata(dataset: str | None) -> dict:
    if dataset == "generated":
        return {"id": "generated", "label": "LLM-generated personas", "source": "llm-generated", "count": None}
    if dataset in NEMOTRON_DATASETS:
        path = find_nemotron_path(dataset)
        info = _dataset_info(path) if path else {}
        split = dataset_split_info(info, dataset)
        return {"id": dataset, "label": NEMOTRON_LABELS.get(dataset, dataset), "source": "nemotron", "count": split.get("num_examples"),
                "path": str(path) if path else str(dataset_path(dataset)), "ready": path is not None}
    return {"id": dataset, "label": dataset or "Automatic", "source": "unknown", "count": None}


class CohortConfig(BaseModel):
    session_id: str | None = None
    description: str
    audience_context: str = ""
    segments: list[dict]  # [{"label": "...", "count": N}, ...]
    parallel: int = 2
    dataset: str | None = None


class EvalConfig(BaseModel):
    session_id: str
    parallel: int = 2


class CounterfactualConfig(BaseModel):
    session_id: str
    changes: list[dict]  # [{"id": "...", "label": "...", "description": "..."}, ...]
    min_score: int = 4
    max_score: int = 7
    parallel: int = 2


class CalibrationAnchor(BaseModel):
    mean_score: float
    metric_value: float

class CalibrationInput(BaseModel):
    metric_name: str = "conversion rate"
    metric_unit: str = "%"
    anchors: list[CalibrationAnchor]  # At least 1; first is "current entity"

class SuggestSegmentsInput(BaseModel):
    entity_text: str
    audience_context: str


class LoginEmail(BaseModel):
    email: str

class LoginCode(LoginEmail):
    code: str

@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, error: AuthError):
    return JSONResponse({"detail": str(error)}, status_code=error.status,
                        headers={"Retry-After": str(error.retry_after)} if error.retry_after else None)

@app.get("/healthz")
async def health():
    return {"status": "ok"}

@app.get("/login")
async def login_page():
    return FileResponse(Path(__file__).parent / "static" / "login.html")

@app.post("/auth/request-code")
async def request_login_code(input: LoginEmail, request: Request):
    await asyncio.to_thread(request.state.auth.request_code, input.email, request.client.host)
    return {"message": "如果该邮箱已获准访问，你将收到验证码。"}

@app.post("/auth/verify")
async def verify_login_code(input: LoginCode, request: Request):
    token = await asyncio.to_thread(request.state.auth.verify, input.email, input.code, request.client.host)
    response = JSONResponse({"ok": True})
    response.set_cookie(request.state.cookie_name, token, max_age=86400, httponly=True,
                        secure=request.state.secure_cookie, samesite="strict", path="/")
    return response

@app.get("/auth/me")
async def current_login(request: Request):
    if not request.state.user:
        raise HTTPException(401, "Login required")
    return request.state.user

@app.post("/auth/logout")
async def logout(request: Request):
    request.state.auth.logout(request.cookies.get(request.state.cookie_name))
    response = JSONResponse({"ok": True})
    response.delete_cookie(request.state.cookie_name, path="/", httponly=True,
                           secure=request.state.secure_cookie, samesite="strict")
    return response


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
async def get_config():
    """Return current LLM config and truthful per-dataset readiness."""
    has_key = bool(os.getenv("LLM_API_KEY"))  # server-level key only; per-session keys checked client-side
    records = []
    for dataset, hf_name in NEMOTRON_DATASETS.items():
        path = find_nemotron_path(dataset)
        info = _dataset_info(path) if path else {}
        split = dataset_split_info(info, dataset)
        records.append({
            "id": dataset, "label": NEMOTRON_LABELS.get(dataset, dataset), "hf_name": hf_name,
            "status": "ready" if path else "available", "ready": path is not None,
            "count": split.get("num_examples") if isinstance(split, dict) else None,
            "path": str(path) if path else str(dataset_path(dataset)),
            "default": dataset == "USA", "source": "nemotron",
        })
    records.append({
        "id": "generated", "label": "LLM-generated personas", "hf_name": None,
        "status": "available", "ready": True, "count": None, "path": None,
        "default": False, "source": "llm-generated",
    })
    usa = next(item for item in records if item["id"] == "USA")
    return {
        "model": get_model(),
        "has_api_key": has_key,
        "base_url": os.getenv("LLM_BASE_URL", ""),
        "nemotron_available": usa["ready"],
        "is_spaces": IS_SPACES,
        "persona_datasets": records,
    }



class SuggestChangesInput(BaseModel):
    entity_text: str
    goal: str
    concerns: list[str]


class NemotronPathInput(BaseModel):
    path: str | None = None
    dataset: str = "USA"


@app.post("/api/nemotron/setup")
async def setup_nemotron(input: NemotronPathInput):
    """Load or download a country into its own validated directory."""
    if input.dataset not in NEMOTRON_DATASETS:
        raise HTTPException(400, f"Unknown dataset '{input.dataset}'. Choose a listed country.")
    p = Path(input.path).expanduser().resolve() if input.path else dataset_path(input.dataset).resolve()
    # Prevent path traversal — must be within project or /tmp
    allowed_tmp = Path("/tmp").resolve()
    if not (p.is_relative_to(PROJECT_ROOT.resolve()) or p.is_relative_to(allowed_tmp) or p.is_relative_to(Path("/data"))):
        raise HTTPException(403, "Path must be within the project directory")
    hf_name = NEMOTRON_DATASETS[input.dataset]
    setup_lock = _setup_lock_for(p)
    if not setup_lock.acquire(blocking=False):
        raise HTTPException(409, f"Dataset setup is already in progress for {p}. Try again shortly.")

    try:
        if (p / "dataset_info.json").exists():
            try:
                validate_dataset_identity(p, input.dataset)
            except DatasetIdentityError as e:
                raise HTTPException(409, str(e))
            if not _dataset_is_complete(p):
                raise HTTPException(
                    409,
                    f"The existing {input.dataset} folder is incomplete at {p}. "
                    "Choose a separate empty folder; existing data was preserved.",
                )
            ds = await asyncio.to_thread(get_nemotron, input.dataset, str(p))
            if ds is None:
                raise HTTPException(500, f"Failed to load {input.dataset} dataset; choose a separate folder to retry")
            _dataset_paths[input.dataset] = p
            return {"status": "loaded", "path": str(p), "count": len(ds), "dataset": input.dataset,
                    "source": "nemotron", "label": NEMOTRON_LABELS.get(input.dataset, input.dataset)}

        if p.exists() and (not p.is_dir() or any(p.iterdir())):
            raise HTTPException(
                409,
                f"The target folder {p} already contains unrelated or incomplete data. "
                "Choose a separate empty folder; existing data was preserved.",
            )

        # Download from HuggingFace into a sibling temporary directory, then atomically publish.
        try:
            from datasets import load_dataset
            parent = p.parent
            parent.mkdir(parents=True, exist_ok=True)
            tmp = parent / f".{p.name}.download-{uuid.uuid4().hex[:8]}"
            try:
                split_name = dataset_split_name(input.dataset)
                if input.dataset in NEMOTRON_CONFIGS:
                    ds = await asyncio.to_thread(
                        load_dataset, hf_name, NEMOTRON_CONFIGS[input.dataset], split=split_name
                    )
                else:
                    ds = await asyncio.to_thread(load_dataset, hf_name, split=split_name)
                await asyncio.to_thread(ds.save_to_disk, str(tmp))
                validate_dataset_identity(tmp, input.dataset)
                if not _dataset_is_complete(tmp):
                    raise RuntimeError("Downloaded dataset is incomplete")
                tmp.replace(p)
            finally:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
            loaded = await asyncio.to_thread(get_nemotron, input.dataset, str(p))
            if loaded is None:
                raise RuntimeError(f"Downloaded {input.dataset}, but loading it failed")
            _dataset_paths[input.dataset] = p
            return {"status": "downloaded", "path": str(p), "count": len(loaded), "dataset": input.dataset,
                    "source": "nemotron", "label": NEMOTRON_LABELS.get(input.dataset, input.dataset)}
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
            if isinstance(e, DatasetIdentityError):
                raise HTTPException(409, str(e))
            raise HTTPException(500, "Dataset could not be loaded. Contact the administrator.")
    finally:
        setup_lock.release()


@app.post("/api/session")
async def create_session(entity: EntityInput, request: Request = None):
    """Create a new evaluation session with an entity."""
    if entity.dataset and entity.dataset != "generated":
        if entity.dataset not in NEMOTRON_DATASETS:
            raise HTTPException(400, f"Unknown dataset '{entity.dataset}'. Choose a listed country or generated.")
        if find_nemotron_path(entity.dataset) is None:
            raise HTTPException(409, f"{entity.dataset} is not installed. Load it from the dataset panel first.")
    owner = request.state.user["email"] if request else None
    expired = [sid for sid, item in sessions.items() if time.time() - item.get("created_ts", 0) >= 86400]
    for sid in expired:
        sessions.pop(sid, None)
    if len(sessions) >= 100 or sum(item.get("owner") == owner for item in sessions.values()) >= 10:
        raise HTTPException(429, "Too many active reviews. Try again tomorrow.")
    sid = uuid.uuid4().hex
    log.info(f"New session {sid} ({len(entity.entity_text)} chars)")
    sessions[sid] = {
        "id": sid,
        "owner": owner,
        "created_ts": time.time(),
        "entity_text": entity.entity_text,
        "goal": "",
        "audience": "",
        "cohort": None,
        "eval_results": None,
        "gradient": None,
        "gradient_ranked": None,
        "bias_audit": None,
        "calibration": None,
        "created": datetime.now().isoformat(),
        "dataset": dataset_metadata(entity.dataset) if entity.dataset else None,
    }
    return {"session_id": sid}


class SessionMetaUpdate(BaseModel):
    goal: str = ""
    audience: str = ""


@app.patch("/api/session/{sid}")
async def update_session_meta(sid: str, meta: SessionMetaUpdate):
    """Update session metadata (goal, audience)."""
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    if meta.goal:
        sessions[sid]["goal"] = meta.goal
    if meta.audience:
        sessions[sid]["audience"] = meta.audience
    return {"ok": True}


@app.post("/api/calibrate/{sid}")
async def set_calibration(sid: str, cal: CalibrationInput):
    """Set metric calibration for a session. Requires eval results."""
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[sid]
    if not session["eval_results"]:
        raise HTTPException(400, "Run evaluation first")

    anchors = [{"mean_score": a.mean_score, "metric_value": a.metric_value}
               for a in cal.anchors if a.metric_value > 0]
    if not anchors:
        raise HTTPException(400, "Need at least one anchor with metric_value > 0")
    if any(a["mean_score"] <= 0 for a in anchors):
        raise HTTPException(400, "Mean score must be positive")

    if len(anchors) == 1:
        # Single anchor: linear scaling. metric = k * mean_score
        k = anchors[0]["metric_value"] / anchors[0]["mean_score"]
        session["calibration"] = {
            "metric_name": cal.metric_name,
            "metric_unit": cal.metric_unit,
            "method": "linear",
            "k": k,
            "anchors": anchors,
        }
    else:
        # 2+ anchors: Platt scaling
        platt_anchors = [{"mean_score": a["mean_score"], "real_ctr": a["metric_value"]}
                         for a in anchors]
        a, b = fit_platt_scaling(platt_anchors)
        session["calibration"] = {
            "metric_name": cal.metric_name,
            "metric_unit": cal.metric_unit,
            "method": "platt",
            "a": a, "b": b,
            "anchors": anchors,
        }

    # Re-calibrate existing gradient if available
    result = _apply_calibration(session)
    return {"ok": True, "calibration": session["calibration"], "calibrated_gradient": result}


@app.delete("/api/calibrate/{sid}")
async def clear_calibration(sid: str):
    """Remove metric calibration from a session."""
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    sessions[sid]["calibration"] = None
    return {"ok": True}


def _apply_calibration(session):
    """Apply calibration to existing gradient data. Returns calibrated ranked list or None."""
    cal = session.get("calibration")
    ranked = session.get("gradient_ranked")
    if not cal or not ranked:
        return None

    valid = [r for r in (session.get("eval_results") or []) if r and isinstance(r.get("score"), (int, float))]
    if not valid:
        return None
    mean_score = sum(r["score"] for r in valid) / len(valid)

    if cal["method"] == "linear":
        k = cal["k"]
        current_metric = k * mean_score
        result = []
        for r in ranked:
            metric_delta = r["avg_delta"] * k
            result.append({
                "id": r["id"],
                "label": r["label"],
                "avg_delta": r["avg_delta"],
                "metric_delta": round(metric_delta, 4),
                "predicted_metric": round(current_metric + metric_delta, 4),
            })
        return {"current_metric": round(current_metric, 4), "items": result}
    elif cal["method"] == "platt":
        a, b = cal["a"], cal["b"]
        current_metric = predict_ctr(a, b, mean_score)
        deriv = ctr_derivative(a, b, mean_score)
        result = []
        for r in ranked:
            metric_delta = r["avg_delta"] * deriv
            result.append({
                "id": r["id"],
                "label": r["label"],
                "avg_delta": r["avg_delta"],
                "metric_delta": round(metric_delta, 4),
                "predicted_metric": round(current_metric + metric_delta, 4),
            })
        return {"current_metric": round(current_metric, 4), "items": result}
    return None


@app.get("/api/session/{sid}")
async def get_session(sid: str):
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    s = sessions[sid]
    return {
        "id": s["id"],
        "has_cohort": s["cohort"] is not None,
        "cohort_size": len(s["cohort"]) if s["cohort"] else 0,
        "has_eval": s["eval_results"] is not None,
        "has_gradient": s["gradient"] is not None,
        "dataset": s.get("dataset"),
    }


class InferSpecInput(BaseModel):
    entity_text: str


@app.post("/api/infer-spec")
async def infer_spec(input: InferSpecInput, request: Request):
    """Infer goal and audience from entity text."""
    log.info(f"Infer spec ({request.client.host})")
    client, _ = llm_from_request(request)
    model = get_fast_model()

    prompt = f"""Read this entity and infer two things:
1. What is the most likely GOAL the author has? (what outcome they want)
2. Who is the intended AUDIENCE? (who evaluates or decides)

Entity:
{input.entity_text[:2000]}

Return JSON:
{{
    "goal": "<1 sentence — the outcome they're optimizing for>",
    "audience": "<1 sentence — who should evaluate this, with demographics if obvious>"
}}

Examples:
- Product landing page → goal: "Convert visitors to paying customers", audience: "Software developers evaluating dev tools"
- Resume → goal: "Get interview callbacks from target companies", audience: "Engineering hiring managers at mid-stage startups"
- Professional bio → goal: "Build credibility and attract inbound opportunities", audience: "Industry peers and potential collaborators"
- Pitch deck → goal: "Secure Series A funding", audience: "VCs and angels focused on B2B SaaS"

Be specific to THIS entity, not generic."""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=256,
            temperature=0.5,
        )
        content = resp.choices[0].message.content
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return json.loads(content)
    except Exception as e:
        raise HTTPException(500, f"Failed to infer spec: {e}")


@app.post("/api/suggest-changes")
async def suggest_changes(input: SuggestChangesInput, request: Request):
    """Generate candidate changes from evaluation concerns and goal."""
    log.info(f"Suggest changes ({len(input.concerns)} concerns)")
    client, _ = llm_from_request(request)
    model = get_fast_model()

    concerns_text = "\n".join(f"- {c}" for c in input.concerns[:15])
    prompt = f"""Based on these evaluation results, suggest at most 3 specific, actionable changes.

Entity (first 1000 chars):
{input.entity_text[:1000]}

Goal: {input.goal or 'Improve overall reception'}

Top concerns from the persuadable middle (people who scored 4-7):
{concerns_text}

For each change, suggest something that directly addresses one or more concerns.
Only suggest changes the entity owner could realistically make.
Do NOT suggest changes that would fundamentally alter the entity's identity.

Return JSON:
{{
    "changes": [
        {{"id": "change_1", "label": "<short label>", "description": "<what specifically changes, 1-2 sentences>"}}
    ]
}}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1024,
            temperature=0.7,
        )
        content = resp.choices[0].message.content
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return json.loads(content)
    except Exception as e:
        raise HTTPException(500, f"Failed to suggest changes: {e}")


@app.post("/api/suggest-segments")
async def suggest_segments(input: SuggestSegmentsInput, request: Request):
    """Use LLM to suggest audience segments based on entity and context."""
    log.info("Suggest segments")
    client, _ = llm_from_request(request)
    model = get_fast_model()

    prompt = f"""Given this entity and audience context, suggest 4-5 evaluator segments.
Each segment should represent a distinct perspective that would evaluate this entity differently.

Entity:
{input.entity_text[:2000]}

Audience context: {input.audience_context}

Return JSON:
{{
    "segments": [
        {{"label": "<concise segment description, 5-10 words>", "count": <6-10>}}
    ]
}}

Make segments specific to THIS domain. For a product, use buyer personas.
For a resume, use different hiring managers. For a pitch, use different investor types. Etc.
Be concrete and relevant — no generic segments."""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1024,
            temperature=0.7,
        )
        content = resp.choices[0].message.content
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        data = json.loads(content)
        return data
    except Exception as e:
        raise HTTPException(500, f"Failed to suggest segments: {e}")


def _dataset_sample_values(ds, columns: set[str], sample_size: int = 32) -> dict[str, list[str]]:
    """Collect short scalar examples without scanning a full dataset."""
    try:
        sample = ds.select(range(min(sample_size, len(ds))))
    except (AttributeError, TypeError, ValueError):
        return {}
    values = {}
    for column in sorted(columns):
        try:
            raw_values = sample[column]
        except (KeyError, TypeError, ValueError):
            continue
        seen = []
        for value in raw_values:
            if value is None or isinstance(value, (dict, list, tuple)):
                continue
            text = str(value)
            if len(text) > 80 or text in seen:
                continue
            seen.append(text)
            if len(seen) >= 8:
                break
        if seen:
            values[column] = seen
    return values


def extract_filters(client, model, audience_context, entity_text="", schema=None,
                    dataset="USA", columns=None, sample_values=None):
    """Use the selected dataset's native columns and values to extract filters."""
    if not audience_context.strip():
        return {}
    columns = set(columns or schema or ())
    sample_values = sample_values or {}
    schema_line = ", ".join(sorted(columns)) or "none"
    examples = "\n".join(
        f"- {name}: {', '.join(values)}" for name, values in sorted(sample_values.items())
    ) or "(no scalar examples available)"
    prompt = (
        "Extract structured demographic filters from this audience description.\n"
        "Include only explicitly stated constraints. Do not infer sex, geography, age, education, or occupational proxies from a profession or product. Observed values are examples, not defaults. If a role has no exact dataset equivalent, omit the occupation filter; never substitute an unrelated role or not_in_workforce.\n\n"
        f"Selected dataset: {dataset}\n"
        f"Available columns: {schema_line}\n"
        "Observed scalar values (use exact native spelling, including native sex values):\n"
        f"{examples}\n\n"
        f"Audience: {audience_context}\n"
        f"Entity context: {entity_text[:500]}\n\n"
        "Return JSON only. Keys must be available columns above, plus age_min and age_max. "
        "Use the actual geography column names (for example prefecture, region, area, "
        "commune, departement, district, or state) instead of inventing city/state keys. "
        "Return {\"filters\": {field: value}, \"evidence\": {field: exact_quote_from_Audience}}. Every filter needs a verbatim supporting quote from Audience (not Entity context or examples). Omit unspecified fields. Use empty objects if no explicit constraints exist."
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1024,
            temperature=0.2,
        )
        content = resp.choices[0].message.content
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        payload = json.loads(content)
        if not isinstance(payload, dict) or "filters" not in payload or "evidence" not in payload:
            raise ValueError("Invalid filter response")
        filters = payload.get("filters", {})
        evidence = payload.get("evidence", {})
        if not isinstance(filters, dict) or not isinstance(evidence, dict):
            raise ValueError("Invalid filter objects")
        def normalized(value):
            return " ".join(re.findall(r"\w+", str(value).casefold().replace("_", " ")))
        aliases = {
            "male": ("male", "men", "males", "男性", "男"),
            "female": ("female", "women", "females", "女性", "女"),
            "男": ("male", "men", "男性", "男"),
            "女": ("female", "women", "女性", "女"),
            "ca": ("ca", "california"), "usa": ("usa", "us", "united states"),
            "関東": ("関東", "kanto", "kantō"),
        }
        clean = {}
        for key, value in filters.items():
            quote = evidence.get(key)
            if not isinstance(quote, str) or not quote.strip():
                continue
            occurrence = re.search(r"(?<![A-Za-z0-9])" + re.escape(quote) + r"(?![A-Za-z0-9])", audience_context, re.IGNORECASE)
            if occurrence is None:
                continue
            if key in {"age_min", "age_max"}:
                nearby = normalized(audience_context[max(0, occurrence.start()-20):occurrence.end()+20])
                if not re.search(r"\bage(?:d|s)?\b|years? old|岁|年龄", nearby):
                    continue
            if columns and key not in columns and key not in {"age_min", "age_max"}:
                continue
            if key in {"age_min", "age_max"} and (type(value) is not int or not 0 <= value <= 120):
                continue
            values = value if isinstance(value, list) else [value]
            if not values or any(not isinstance(item, (str, int)) or isinstance(item, bool) for item in values):
                continue
            quote_words = " " + normalized(quote) + " "
            if all(any(" " + normalized(alias) + " " in quote_words for alias in aliases.get(normalized(item), (item,))) for item in values):
                clean[key] = value
        return clean
    except Exception:
        raise HTTPException(502, "Could not interpret audience filters. Please retry; no broader population was substituted.") from None


@app.post("/api/cohort/generate")
async def generate_cohort_endpoint(config: CohortConfig, request: Request):
    """Generate a cohort and optionally attach it directly to its owner's review."""
    target = None
    if config.session_id:
        target = sessions.get(config.session_id)
        user = getattr(request.state, "user", None)
        if not target or not user or target.get("owner") != user["email"]:
            raise HTTPException(404, "Session not found")
    total = sum(s.get("count", 8) for s in config.segments)
    log.info(f"Generate cohort: {total} personas, {len(config.segments)} segments")

    filters = {}
    selected = config.dataset
    if selected == "generated":
        ds = None
    elif selected:
        if selected not in NEMOTRON_DATASETS:
            raise HTTPException(400, f"Unknown dataset '{selected}'. Choose a listed country or generated.")
        if find_nemotron_path(selected) is None:
            raise HTTPException(409, f"{selected} is not installed. Load it from the dataset panel first.")
        ds = await asyncio.to_thread(get_nemotron, selected)
        if ds is None:
            raise HTTPException(503, f"{selected} is installed but could not be loaded. Try loading it again.")
    else:
        selected = "USA" if find_nemotron_path("USA") else "generated"
        ds = await asyncio.to_thread(get_nemotron, "USA") if selected == "USA" else None
    if ds is not None:
        # Use census-grounded Nemotron personas
        import random
        pl = _lazy_persona_loader()
        ss = _lazy_stratified_sampler()

        # Extract structured filters from the selected dataset's native schema.
        columns = set(getattr(ds, "column_names", []))
        sample_values = _dataset_sample_values(ds, columns)
        client, model = llm_from_request(request)
        filters = extract_filters(
            client, get_fast_model(), config.audience_context, config.description,
            dataset=selected, columns=columns, sample_values=sample_values,
        )
        log.info("Dataset audience filters generated")

        unsupported = [key for key in filters if key not in columns and key not in {"age_min", "age_max"}]
        if unsupported:
            raise HTTPException(400, "Selected dataset does not support these audience filters: " + ", ".join(unsupported))
        try:
            filtered = pl.filter_personas(ds, filters, limit=max(total * 20, 2000))
        except (KeyError, TypeError) as e:
            raise HTTPException(400, f"Selected dataset cannot apply the requested audience filters: {e}")
        if len(filtered) == 0:
            raise HTTPException(400, "No dataset personas match this audience. Choose LLM-generated personas or revise the audience filters.")
        profiles = [pl.to_profile(row, i, dataset=selected) for i, row in enumerate(filtered)]

        # Use only age + education to keep strata count < total
        dim_fns = [
            lambda p: ss.age_bracket(p.get("age", 30)),
            lambda p: p.get("education_level", "") or "unknown",
        ]
        diversity_fn = lambda p: p.get("occupation", "unknown") or "unknown"

        all_personas = ss.stratified_sample(profiles, dim_fns, total=total,
                                            diversity_fn=diversity_fn)
        # Hard cap — stratified_sample can exceed total when strata > total
        if len(all_personas) > total:
            random.seed(42)
            all_personas = random.sample(all_personas, total)
        source = "nemotron"
    else:
        # Fallback: LLM-generated
        client, model = llm_from_request(request)
        all_personas = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=config.parallel) as pool:
            # Two profiles fit the web client's 2,048-token response budget.
            batches = [(seg, min(2, seg["count"] - start), start // 2 + 1)
                       for seg in config.segments for start in range(0, seg["count"], 2)]
            futs = {
                pool.submit(generate_segment, client, model, seg["label"], count,
                            f"{config.description}\nPanel batch {batch}: use varied names and backgrounds."): count
                for seg, count, batch in batches
            }
            for fut in concurrent.futures.as_completed(futs):
                personas = fut.result()
                if isinstance(personas, list):
                    all_personas.extend(dict(p) for p in personas[:futs[fut]] if isinstance(p, dict))
        source = "llm-generated"

    # Model output can overproduce profiles; honor the requested panel size.
    all_personas = all_personas[:min(total, 50)]
    if not all_personas or any(not isinstance(persona, dict) for persona in all_personas):
        raise HTTPException(502, "No usable personas returned. Please try again.")
    for i, p in enumerate(all_personas):
        p["user_id"] = i

    if target is not None:
        if sessions.get(config.session_id) is not target:
            raise HTTPException(409, "Review expired while building the panel. Start a new review.")
        target["cohort"] = all_personas
        target["dataset"] = dataset_metadata(selected)

    return {
        "cohort_size": len(all_personas),
        "cohort_saved": target is not None,
        "matching_note": "Dataset sampling uses explicit demographic filters; professional-role matching is not guaranteed." if ds is not None else None,
        **({} if target is not None else {"cohort": all_personas}),
        "source": source,
        "dataset": selected,
        "source_label": dataset_metadata(selected)["label"],
        "filters": filters if ds is not None else None,
    }


@app.post("/api/cohort/upload/{sid}")
async def upload_cohort(sid: str, cohort: list[dict], dataset: str | None = Query(None)):
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    sessions[sid]["cohort"] = cohort
    if dataset:
        sessions[sid]["dataset"] = dataset_metadata(dataset)
    return {"cohort_size": len(cohort)}


# ── SSE streaming endpoints ──────────────────────────────────────────────

@app.get("/api/evaluate/stream/{sid}")
async def evaluate_stream(sid: str, request: Request, parallel: int = 2,
                          bias_calibration: bool = False):
    """Run evaluation with Server-Sent Events for real-time progress."""
    # Persistent per-user quotas are enforced at the ASGI boundary.
    log.info(f"Evaluate stream {sid}")
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[sid]
    if not session["cohort"]:
        raise HTTPException(400, "No cohort — generate or upload one first")
    parallel = min(parallel, 10)

    # Capture LLM config from request headers before entering async generator
    _api_key = request.state.api_key
    _base_url = request.state.base_url
    _model = request.state.model

    async def event_generator():
        client, mdl = _llm_from_params(_api_key, _base_url, _model)
        cohort = session["cohort"]
        entity_text = session["entity_text"]
        total = len(cohort)
        sys_prompt = SYSTEM_PROMPT + BIAS_CALIBRATION_ADDENDUM if bias_calibration else None

        yield {"event": "start", "data": json.dumps({
            "total": total, "model": mdl,
            "bias_calibration": bias_calibration,
            "dataset": session.get("dataset"),
        })}

        results = [None] * total
        done = 0
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futs = {
                pool.submit(evaluate_one, client, mdl, ev, entity_text,
                            system_prompt=sys_prompt): i
                for i, ev in enumerate(cohort)
            }
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {"error": str(e), "_evaluator": {"name": "?"}}
                results[idx] = result
                done += 1

                ev = result.get("_evaluator", {})
                progress = {
                    "done": done,
                    "total": total,
                    "name": ev.get("name", "?"),
                    "score": result.get("score"),
                    "action": result.get("action"),
                    "error": result.get("error"),
                }
                yield {"event": "progress", "data": json.dumps(progress)}

        elapsed = time.time() - t0
        session["eval_results"] = results

        analysis = analyze_eval(results)
        valid = [r for r in results if isinstance(r.get("score"), (int, float)) and not isinstance(r.get("score"), bool)]
        scores = [r["score"] for r in valid]
        avg = sum(scores) / len(scores) if scores else 0
        actions = [r["action"] for r in valid]

        summary = {
            "elapsed": round(elapsed, 1),
            "total": len(valid),
            "avg_score": round(avg, 1),
            "positive": actions.count("positive"),
            "neutral": actions.count("neutral"),
            "negative": actions.count("negative"),
            "analysis": analysis,
            "results": results,
            "dataset": session.get("dataset"),
        }
        if not scores:
            summary["error"] = "No valid numeric scores from evaluators"
        yield {"event": "complete", "data": json.dumps(summary)}

    return EventSourceResponse(event_generator(), ping=15)


class CounterfactualRequest(BaseModel):
    changes: list[dict]
    goal: str = ""
    min_score: int = 4
    max_score: int = 7
    parallel: int = 2


# Store pending counterfactual configs for SSE pickup (with timestamps)
_cf_pending: dict = {}  # ticket -> {"req": CounterfactualRequest, "ts": time.time()}


@app.post("/api/counterfactual/prepare/{sid}")
async def prepare_counterfactual(sid: str, req: CounterfactualRequest):
    """Stage counterfactual config, return a ticket for the SSE stream."""
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    ticket = uuid.uuid4().hex
    # Clean expired tickets (>10 min)
    now = time.time()
    expired = [k for k, v in _cf_pending.items() if now - v.get("ts", 0) > 600]
    for k in expired:
        del _cf_pending[k]
    if len(_cf_pending) >= 100 or sum(v.get("sid") == sid for v in _cf_pending.values()) >= 3:
        raise HTTPException(429, "Too many pending comparisons")
    _cf_pending[ticket] = {"req": req, "ts": now, "sid": sid}
    return {"ticket": ticket}


@app.get("/api/counterfactual/stream/{sid}")
async def counterfactual_stream(sid: str, ticket: str, request: Request):
    """Run counterfactual probes with SSE progress."""
    # Persistent per-user quotas are enforced at the ASGI boundary.
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[sid]
    if not session["eval_results"]:
        raise HTTPException(400, "Run evaluation first")
    entry = _cf_pending.pop(ticket, None)
    if not entry or time.time() - entry.get("ts", 0) >= 600:
        raise HTTPException(400, "Invalid or expired ticket")
    if entry.get("sid") != sid:
        raise HTTPException(403, "Ticket does not belong to this session")
    req = entry["req"]

    all_changes = req.changes
    goal = req.goal
    min_score = req.min_score
    max_score = req.max_score
    parallel = req.parallel

    _api_key = request.state.api_key
    _base_url = request.state.base_url
    _model = request.state.model
    parallel = min(req.parallel, 10)

    async def event_generator():
        client, mdl = _llm_from_params(_api_key, _base_url, _model)
        cohort = session["cohort"]
        eval_results = session["eval_results"]
        cohort_map = {f"{p.get('name','')}_{p.get('user_id','')}": p for p in cohort}

        movable = [r for r in eval_results
                   if "score" in r and min_score <= r["score"] <= max_score]

        total = len(movable)
        has_goal = bool(goal.strip())
        yield {"event": "start", "data": json.dumps({
            "total": total, "changes": len(all_changes), "model": mdl,
            "goal": goal if has_goal else None,
        })}

        if total == 0:
            yield {"event": "complete", "data": json.dumps({
                "error": "No evaluators in movable middle",
                "gradient": "",
                "results": [],
            })}
            return

        # Compute goal-relevance weights (VJP) if goal is set
        goal_weights = None
        if has_goal:
            yield {"event": "goal_weights", "data": json.dumps({
                "status": "computing", "message": "Scoring evaluator relevance to goal..."
            })}
            goal_weights = compute_goal_weights(
                client, mdl, eval_results, cohort_map, goal, parallel=parallel,
            )
            relevant = sum(1 for v in goal_weights.values() if v["weight"] >= 0.5)
            yield {"event": "goal_weights", "data": json.dumps({
                "status": "done",
                "relevant": relevant,
                "total": len(goal_weights),
                "message": f"{relevant}/{len(goal_weights)} evaluators relevant to goal",
            })}

        results = [None] * total
        done = 0
        t0 = time.time()

        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futs = {
                pool.submit(probe_one, client, mdl, r, cohort_map, all_changes): i
                for i, r in enumerate(movable)
            }
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {"error": str(e), "_evaluator": {"name": "?"}}
                results[idx] = result
                done += 1

                ev = result.get("_evaluator", {})
                cfs = result.get("counterfactuals", [])
                top = max(cfs, key=lambda c: c.get("delta", 0)) if cfs else {}
                progress = {
                    "done": done,
                    "total": total,
                    "name": ev.get("name", "?"),
                    "original_score": result.get("original_score"),
                    "best_delta": top.get("delta", 0),
                    "best_change": top.get("change_id", "?"),
                    "error": result.get("error"),
                }
                yield {"event": "progress", "data": json.dumps(progress)}

        elapsed = time.time() - t0
        gradient_text, ranked_data = analyze_gradient(results, all_changes,
                                                      goal_weights=goal_weights)
        session["gradient"] = gradient_text
        session["gradient_ranked"] = ranked_data

        # Apply metric calibration if set
        calibrated = _apply_calibration(session)

        yield {"event": "complete", "data": json.dumps({
            "elapsed": round(elapsed, 1),
            "gradient": gradient_text,
            "ranked": ranked_data,
            "results": results,
            "goal": goal if has_goal else None,
            "calibrated": calibrated,
            "calibration": session.get("calibration"),
        })}

    return EventSourceResponse(event_generator(), ping=15)


@app.get("/api/bias-audit/stream/{sid}")
async def bias_audit_stream(
    sid: str, request: Request, probes: str = "framing,authority,order",
    sample: int = 5, parallel: int = 2
):
    """Run bias audit probes with SSE progress."""
    # Persistent per-user quotas are enforced at the ASGI boundary.
    parallel = min(parallel, 10)
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    session = sessions[sid]
    if not session["cohort"]:
        raise HTTPException(400, "No cohort — generate or upload one first")

    probe_list = [p.strip() for p in probes.split(",") if p.strip()]

    async def event_generator():
        import random
        _api_key = request.state.api_key
        _base_url = request.state.base_url
        _model = request.state.model
        client, mdl = _llm_from_params(_api_key, _base_url, _model)
        cohort = session["cohort"]
        entity_text = session["entity_text"]

        random.seed(42)
        evaluators = random.sample(cohort, min(sample, len(cohort)))

        yield {"event": "start", "data": json.dumps({
            "probes": probe_list,
            "sample_size": len(evaluators),
            "model": mdl,
        })}

        all_analyses = []

        for probe_name in probe_list:
            yield {"event": "probe_start", "data": json.dumps({"probe": probe_name})}

            t0 = time.time()

            if probe_name == "framing":
                gain_entity = reframe_entity(client, mdl, entity_text, "gain")
                loss_entity = reframe_entity(client, mdl, entity_text, "loss")
                results = run_paired_evaluation(
                    client, mdl, evaluators, gain_entity, loss_entity,
                    "gain", "loss", parallel,
                )
                label_a, label_b = "gain", "loss"
            elif probe_name == "authority":
                entity_with_auth = add_authority_signals(entity_text)
                results = run_paired_evaluation(
                    client, mdl, evaluators, entity_text, entity_with_auth,
                    "baseline", "authority", parallel,
                )
                label_a, label_b = "baseline", "authority"
            elif probe_name == "order":
                reordered = reorder_entity(entity_text)
                results = run_paired_evaluation(
                    client, mdl, evaluators, entity_text, reordered,
                    "original", "reordered", parallel,
                )
                label_a, label_b = "original", "reordered"
            else:
                continue

            elapsed = time.time() - t0
            analysis = analyze_probe(results, probe_name, label_a, label_b)
            analysis["elapsed_s"] = round(elapsed, 1)
            all_analyses.append(analysis)

            yield {"event": "probe_complete", "data": json.dumps({
                "probe": probe_name,
                "analysis": analysis,
            })}

        report = generate_report(all_analyses, mdl)
        session["bias_audit"] = {"analyses": all_analyses, "report": report}

        yield {"event": "complete", "data": json.dumps({
            "analyses": all_analyses,
            "report": report,
        })}

    return EventSourceResponse(event_generator(), ping=15)


@app.get("/api/results/{sid}")
async def get_results(sid: str):
    """Get full results for a session."""
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    s = sessions[sid]
    return {
        "eval_results": s["eval_results"],
        "gradient": s["gradient"],
        "cohort": s["cohort"],
        "dataset": s.get("dataset"),
    }


@app.get("/api/report/{sid}")
async def download_report(sid: str):
    """Generate and download a comprehensive markdown report for this session."""
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    s = sessions[sid]
    if not s["eval_results"]:
        raise HTTPException(400, "No evaluation results yet")

    lines = []
    lines.append("# SGO Evaluation Report")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")

    # Entity
    lines.append("---\n")
    lines.append("## Entity Evaluated\n")
    lines.append(s["entity_text"])
    lines.append("")

    if s.get("goal"):
        lines.append(f"**Goal:** {s['goal']}\n")
    if s.get("audience"):
        lines.append(f"**Audience:** {s['audience']}\n")
    if s.get("dataset"):
        dataset = s["dataset"]
        source_label = dataset.get("label", dataset.get("id", "Unknown"))
        count = dataset.get("count")
        lines.append(f"**Persona source:** {source_label}" + (f" ({count:,} available)" if count else "") + "\n")

    # Cohort summary
    cohort = s.get("cohort") or []
    if cohort:
        lines.append("---\n")
        lines.append(f"## Panel ({len(cohort)} evaluators)\n")
        lines.append("| # | Name | Age | Occupation | Location |")
        lines.append("|---|------|-----|------------|----------|")
        for i, p in enumerate(cohort, 1):
            name = p.get("name", "?")
            age = p.get("age", "")
            occ = p.get("occupation", "")
            loc = p.get("city", p.get("location", ""))
            if p.get("state"):
                loc = f"{loc}, {p['state']}" if loc else p["state"]
            lines.append(f"| {i} | {name} | {age} | {occ} | {loc} |")
        lines.append("")

    # Evaluation results
    results = s["eval_results"]
    valid = [r for r in results if r and "score" in r]
    scores = [r["score"] for r in valid]
    avg = sum(scores) / len(scores) if scores else 0

    lines.append("---\n")
    lines.append("## Evaluation Results\n")
    lines.append(f"**Average Score: {avg:.1f}/10** ({len(valid)} evaluators)\n")

    pos = sum(1 for r in valid if r.get("action") == "positive")
    neu = sum(1 for r in valid if r.get("action") == "neutral")
    neg = sum(1 for r in valid if r.get("action") == "negative")
    lines.append(f"- Would say yes: {pos}")
    lines.append(f"- Unsure: {neu}")
    lines.append(f"- Would say no: {neg}\n")

    # Full analysis from evaluate.py
    analysis = analyze_eval(results)
    lines.append(analysis)
    lines.append("")

    # Individual evaluator details
    lines.append("### All Evaluator Responses\n")
    lines.append("| Name | Age | Occupation | Score | Action | Summary |")
    lines.append("|------|-----|------------|-------|--------|---------|")
    sorted_results = sorted(valid, key=lambda r: r["score"], reverse=True)
    for r in sorted_results:
        ev = r.get("_evaluator", {})
        name = ev.get("name", "?")
        age = ev.get("age", "")
        occ = ev.get("occupation", "")
        score = r["score"]
        action = r.get("action", "")
        summary = r.get("summary", "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {name} | {age} | {occ} | {score}/10 | {action} | {summary} |")
    lines.append("")

    # Counterfactual gradient
    if s.get("gradient"):
        lines.append("---\n")
        lines.append("## Priority Actions (Counterfactual Gradient)\n")
        lines.append(s["gradient"])
        lines.append("")

    # Metric calibration
    if s.get("calibration"):
        cal = s["calibration"]
        lines.append("---\n")
        lines.append(f"## Metric Calibration ({cal['metric_name']})\n")
        lines.append(f"- **Method:** {cal['method']}")
        lines.append(f"- **Unit:** {cal['metric_unit']}")
        for anc in cal.get("anchors", []):
            lines.append(f"- Anchor: score {anc['mean_score']:.1f} = {anc['metric_value']}{cal['metric_unit']}")

        calibrated = _apply_calibration(s)
        if calibrated:
            lines.append(f"\n**Current predicted {cal['metric_name']}:** "
                         f"{calibrated['current_metric']}{cal['metric_unit']}\n")
            lines.append(f"| Change | Score Delta | {cal['metric_name']} Delta | Predicted |")
            lines.append("|--------|-----------|-------------|-----------|")
            for item in calibrated["items"]:
                lines.append(
                    f"| {item['label']} | {item['avg_delta']:+.1f} | "
                    f"{item['metric_delta']:+.4f}{cal['metric_unit']} | "
                    f"{item['predicted_metric']}{cal['metric_unit']} |"
                )
            lines.append("")

    # Bias audit
    if s.get("bias_audit"):
        audit = s["bias_audit"]
        lines.append("---\n")
        lines.append("## Panel Realism Check (Bias Audit)\n")
        if audit.get("report"):
            lines.append(audit["report"])
            lines.append("")
        if audit.get("analyses"):
            lines.append("| Probe | Shifted % | Avg Score Change | Human Baseline | Assessment |")
            lines.append("|-------|-----------|------------------|----------------|------------|")
            baselines = {"framing": 30, "authority": 20, "order": 0}
            for a in audit["analyses"]:
                if a.get("error"):
                    continue
                expected = baselines.get(a["probe"])
                gap = a["shifted_pct"] - (expected or 0)
                if expected is not None:
                    if gap > 10:
                        assessment = "Over-biased"
                    elif gap < -10:
                        assessment = "Under-biased"
                    else:
                        assessment = "Well-calibrated"
                else:
                    assessment = "—"
                lines.append(
                    f"| {a['probe']} | {a['shifted_pct']:.1f}% | "
                    f"{a['avg_abs_delta']:.2f} | "
                    f"{str(expected) + '%' if expected is not None else '—'} | "
                    f"{assessment} |"
                )
            lines.append("")

    lines.append("---\n")
    lines.append("*Report generated by [SGO — Semantic Gradient Optimization](https://github.com/anthropics/sgo)*")

    report_md = "\n".join(lines)
    filename = f"sgo-report-{sid}.md"
    return Response(
        content=report_md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "7860" if IS_SPACES else "8000"))
    host = os.getenv("HOST", "127.0.0.1")

    print(f"\n  SGO Web Interface")
    print(f"  http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, access_log=False)

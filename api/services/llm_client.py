import os
import requests
import time
from fastapi import HTTPException

API_BASE_URL = os.getenv("MODEL_BASE_URL", "https://console.labahasa.ai/v1").rstrip("/")
API_KEY      = os.getenv("MODEL_API_KEY", "")
GLM_MODEL    = os.getenv("LLAMA_MODEL", "llama-4-maverick-instruct")
GLM_MAX_ATTEMPTS = int(os.getenv("GLM_MAX_ATTEMPTS", "5"))
GLM_RETRY_BACKOFF_BASE = float(os.getenv("GLM_RETRY_BACKOFF_BASE", "0.8"))

_glm_session = None

GENERIC_GLM_ERROR_MESSAGE = (
    "Layanan AI sedang mengalami gangguan koneksi sementara. "
    "Silakan coba lagi dalam beberapa saat."
)

def get_glm_session() -> requests.Session:
    global _glm_session
    if _glm_session is None:
        _glm_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.packages.urllib3.util.retry.Retry(
                total=2,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["POST"]
            )
        )
        _glm_session.mount("http://", adapter)
        _glm_session.mount("https://", adapter)
    return _glm_session

def is_transient_network_error(e: Exception) -> bool:
    if e is None:
        return False
    err_str = str(e).lower()
    transient_markers = [
        "connection aborted",
        "connection reset",
        "read timed out",
        "connectionrefusederror",
        "timeout",
    ]
    return any(marker in err_str for marker in transient_markers)

def call_glm(messages: list, temperature: float = 0.1, timeout: int = 90) -> str:
    if API_BASE_URL.endswith("/chat/completions"):
        url = API_BASE_URL
    else:
        url = f"{API_BASE_URL}/chat/completions"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False
    }
    max_attempts = max(1, GLM_MAX_ATTEMPTS)
    last_error = None
    session = get_glm_session()

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=(15, timeout))
            if not resp.ok:
                raise HTTPException(status_code=500, detail=GENERIC_GLM_ERROR_MESSAGE)
            return resp.json()["choices"][0]["message"]["content"]
        except HTTPException:
            raise
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_attempts:
                time.sleep(GLM_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            break
        except (KeyError, IndexError, TypeError) as e:
            raise HTTPException(status_code=500, detail=GENERIC_GLM_ERROR_MESSAGE)

    if is_transient_network_error(last_error):
        raise HTTPException(status_code=503, detail=GENERIC_GLM_ERROR_MESSAGE)
    raise HTTPException(status_code=500, detail=GENERIC_GLM_ERROR_MESSAGE)

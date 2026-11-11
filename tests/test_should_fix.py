import pytest
import time
import json
import base64
import io

def test_session_last_seen_throttling():
    from auth import _should_update_last_seen, _SESSION_LAST_SEEN_CACHE

    _SESSION_LAST_SEEN_CACHE.clear()
    uid = "test-user-123"

    # First access updates
    assert _should_update_last_seen(uid) is True
    assert uid in _SESSION_LAST_SEEN_CACHE

    # Immediate second access throttled
    assert _should_update_last_seen(uid) is False

    # Simulate 61 seconds elapsed
    _SESSION_LAST_SEEN_CACHE[uid] = time.time() - 61.0
    assert _should_update_last_seen(uid) is True

    # Cache bound check
    for i in range(5001):
        _SESSION_LAST_SEEN_CACHE[f"dummy-{i}"] = time.time()
    
    # Next call purges oversized cache safely
    _should_update_last_seen("overflow-user")
    assert len(_SESSION_LAST_SEEN_CACHE) <= 2


def test_streaming_base64_json_roundtrip():
    # Test file streaming with non-trivial size and arbitrary byte patterns
    test_bytes = b"PDF-1.7-TEST-STREAM\x00\xff\xfe\x01\x42" * 12000  # ~300KB
    f = io.BytesIO(test_bytes)
    filename = "kontrak_kerjasama.pdf"
    ext = "pdf"

    def iter_file():
        prefix = (json.dumps({"filename": filename, "ext": ext})[:-1] + ', "data": "').encode("utf-8")
        yield prefix
        while True:
            chunk = f.read(48 * 1024)
            if not chunk:
                break
            yield base64.b64encode(chunk)
        yield b'"}'

    streamed_payload = b"".join(iter_file())
    parsed = json.loads(streamed_payload.decode("utf-8"))

    assert parsed["filename"] == filename
    assert parsed["ext"] == ext
    decoded = base64.b64decode(parsed["data"])
    assert decoded == test_bytes





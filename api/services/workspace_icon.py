"""Workspace Lucide icon classification and inference service.

Provides deterministic fast-path regex classification for 12 canonical Indonesian/English
legal contract types, and an LLM fallback (call_glm) for novel workspace names, with
defensive fallback to 'folder'.
"""
import logging
import re
from typing import Optional

try:
    from api.services.llm_client import call_glm, extract_json
except ImportError:
    from services.llm_client import call_glm, extract_json

logger = logging.getLogger(__name__)

# Curated catalog of ~40 valid, verified Lucide icons (kebab-case matching ^[a-z0-9-]+$)
CURATED_WORKSPACE_ICONS: list[str] = [
    # 12 Canonical contract categories & default
    "handshake",
    "clipboard-check",
    "file-plus",
    "share-2",
    "shield-check",
    "lock",
    "gauge",
    "list-checks",
    "file-signature",
    "shopping-cart",
    "file-text",
    "store",
    "folder",
    # Additional curated domain & technical icons
    "award",
    "badge-check",
    "book-open",
    "boxes",
    "briefcase",
    "building-2",
    "calculator",
    "cloud",
    "cpu",
    "credit-card",
    "database",
    "eye-off",
    "file-check",
    "globe",
    "hard-drive",
    "key",
    "landmark",
    "mail",
    "phone",
    "printer",
    "receipt",
    "scale",
    "scroll",
    "search",
    "server",
    "settings",
    "shield",
    "tag",
    "users",
    "zap",
]

_CURATED_ICONS_SET = set(CURATED_WORKSPACE_ICONS)

# 12 Canonical legal contract classification rules with strict word boundary protection (\b)
CANONICAL_ICON_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(pks|perjanjian\s+kerjas?ama|partnership)\b", re.IGNORECASE), "handshake"),
    (re.compile(r"\b(bast|serah\s+terima|handover)\b", re.IGNORECASE), "clipboard-check"),
    (re.compile(r"\b(addendum|amandemen|amendment)\b", re.IGNORECASE), "file-plus"),
    (re.compile(r"\b(data\s+sharing|dsa|berbagi\s+data)\b", re.IGNORECASE), "share-2"),
    (re.compile(r"\b(dpa|data\s+processing|perlindungan\s+data)\b", re.IGNORECASE), "shield-check"),
    (re.compile(r"\b(nda|non[-\s]?disclosure|kerahasiaan|confidential(?:ity)?)\b", re.IGNORECASE), "lock"),
    (re.compile(r"\b(sla|service\s+level|tingkat\s+layanan)\b", re.IGNORECASE), "gauge"),
    (re.compile(r"\b(sow|statement\s+of\s+work|ruang\s+lingkup)\b", re.IGNORECASE), "list-checks"),
    (re.compile(r"\b(mou|nota\s+kesepahaman|memorandum(?:\s+of\s+understanding)?)\b", re.IGNORECASE), "file-signature"),
    (re.compile(r"\b(po|purchase\s+order|pesanan\s+pembelian|purchase\s+agreement)\b", re.IGNORECASE), "shopping-cart"),
    (re.compile(r"\b(spk|surat\s+perintah\s+kerja|work\s+order)\b", re.IGNORECASE), "file-text"),
    (re.compile(r"\b(reseller|keagenan|distributor|mitra)\b", re.IGNORECASE), "store"),
]


def detect_canonical_icon(name: Optional[str]) -> Optional[str]:
    """Check if workspace name matches one of the 12 canonical legal contract types.

    Uses word-boundary patterns (\b) to avoid false-positive matches on common Indonesian words
    (e.g., 'po' in 'Laporan', 'nda' in 'Agenda', 'sla' in 'Legislatif').

    Returns the corresponding Lucide icon name, or None if no canonical pattern matches.
    """
    if not name or not isinstance(name, str):
        return None
    trimmed = name.strip()
    if not trimmed:
        return None
    for pattern, icon in CANONICAL_ICON_RULES:
        if pattern.search(trimmed):
            return icon
    return None


def validate_icon_name(icon: Optional[str]) -> str:
    """Validate that an icon name is present in CURATED_WORKSPACE_ICONS.

    Falls back to 'folder' if invalid, missing, or not whitelisted.
    """
    if not icon or not isinstance(icon, str):
        return "folder"
    cleaned = icon.strip().lower()
    if cleaned in _CURATED_ICONS_SET:
        return cleaned
    return "folder"


def infer_workspace_icon(name: Optional[str], description: str = "") -> str:
    """Infer the most appropriate Lucide icon for a workspace.

    1. Checks fast-path canonical regex matcher (0ms latency, deterministic).
    2. If no canonical match and name is provided, queries the LLM fallback (call_glm)
       with a low temperature and prompt constrained to CURATED_WORKSPACE_ICONS.
    3. Defensively catches any exception or timeout and falls back cleanly to 'folder'.
    """
    if not name or not isinstance(name, str) or not name.strip():
        return "folder"

    clean_name = name.strip()
    canonical = detect_canonical_icon(clean_name)
    if canonical:
        return canonical

    try:
        curated_csv = ", ".join(CURATED_WORKSPACE_ICONS)
        desc_part = (
            f"\nWorkspace Description: {description.strip()}"
            if description and isinstance(description, str) and description.strip()
            else ""
        )
        prompt = (
            f"Choose the single most appropriate Lucide icon name for a legal/corporate workspace.\n"
            f"Workspace Name: {clean_name}{desc_part}\n\n"
            f"Pick strictly ONE icon name from this curated list:\n{curated_csv}\n\n"
            f"Return strictly JSON with key 'icon', for example: {{\"icon\": \"briefcase\"}}"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a legal document classifier that assigns the best matching Lucide icon "
                    "name for a workspace. Respond strictly with a JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        raw_response = call_glm(messages, temperature=0.0, timeout=5)
        parsed = extract_json(raw_response)

        if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
            parsed = parsed[0]

        if isinstance(parsed, dict) and "icon" in parsed:
            return validate_icon_name(parsed["icon"])
        return "folder"
    except Exception as exc:
        logger.warning(
            "LLM workspace icon classification failed for '%s', falling back to 'folder': %s",
            clean_name,
            exc,
        )
        return "folder"

"""Draft-to-draft comparison of clause positions.

A "draft" here is an uploaded file: successive rounds of the same contract live
as sibling documents in one workspace (``compliance_history.notebook_id``,
ordered by ``created_at``). Every upload already carries its own analysis, so
this module only has to line the clauses up and report what moved.

Matching is deterministic and free of model calls:

1. ``favorability.label_key`` + occurrence index ("Pasal 7" can appear twice in
   one contract). This pairs the vast majority of clauses.
2. Leftovers are paired by text similarity (``difflib``, first 200 chars). A
   renumbered clause usually keeps its wording.
3. Anything still unpaired is reported as added / removed.

Direction of change is judged on the commercial axis only; compliance status is
reported per draft so the UI can show "BERESIKO → SESUAI" without the two axes
being blended into one arrow.
"""
from difflib import SequenceMatcher

from services.favorability import label_key

POSISI_RANK = {"MERUGIKAN": 0, "NETRAL": 1, "MENGUNTUNGKAN": 2}
SIMILARITY_FLOOR = 0.6
# Short bodies ("Harga diatur dalam Lampiran.") are textually similar to almost
# anything in the same contract, so similarity pairing only applies to real
# prose. Below this length the label is the only trustworthy signal.
MIN_SIMILARITY_CHARS = 40
PREVIEW_CHARS = 160
MAX_BULLETS = 3


def _cell(clause: dict) -> dict:
    return {
        "present": True,
        "status": clause.get("status", ""),
        "posisi_perusahaan": clause.get("posisi_perusahaan") or "NETRAL",
        "ringkas": str(clause.get("catatan_posisi") or "")[:240],
        "kelebihan": _bullets(clause.get("kelebihan")),
        "kekurangan": _bullets(clause.get("kekurangan")),
        "isi_pasal": str(clause.get("isi_pasal") or "")[:PREVIEW_CHARS],
    }


def _bullets(raw) -> list[dict]:
    out = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("poin"):
            out.append({"poin": str(item["poin"])[:200], "dasar": item.get("dasar") or "komersial"})
        elif isinstance(item, str) and item.strip():
            out.append({"poin": item.strip()[:200], "dasar": "komersial"})
        if len(out) >= MAX_BULLETS:
            break
    return out


def _slug(text: str) -> str:
    return " ".join(str(text or "").lower().split())[:200]


def _pair_by_text(left: list[dict], right: list[dict]) -> list[tuple]:
    """Greedy similarity pairing between two leftover lists (best first)."""
    candidates = []
    for i, a in enumerate(left):
        if len(a["text"]) < MIN_SIMILARITY_CHARS:
            continue
        for j, b in enumerate(right):
            if len(b["text"]) < MIN_SIMILARITY_CHARS:
                continue
            ratio = SequenceMatcher(None, _slug(a["text"]), _slug(b["text"])).ratio()
            if ratio >= SIMILARITY_FLOOR:
                candidates.append((ratio, i, j))
    candidates.sort(reverse=True)

    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs = []
    for _ratio, i, j in candidates:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        pairs.append((left[i]["entry"], right[j]["entry"]))
    return pairs


def _status_change(cells: list[dict]) -> str:
    present = [c for c in cells if c["present"]]
    if len(present) < 2:
        return ""
    first, last = present[0]["status"], present[-1]["status"]
    return "" if first == last else f"{first} → {last}"


def _direction(cells: list[dict]) -> str:
    present = [c for c in cells if c["present"]]
    if not present:
        return "unchanged"
    if not cells[0]["present"]:
        return "added"
    if not cells[-1]["present"]:
        return "removed"
    first = POSISI_RANK.get(present[0]["posisi_perusahaan"], 1)
    last = POSISI_RANK.get(present[-1]["posisi_perusahaan"], 1)
    if last > first:
        return "improved"
    if last < first:
        return "worsened"
    return "unchanged"


def build_comparison(drafts: list[dict]) -> dict:
    """Line up clauses across drafts.

    ``drafts`` must be oldest-first, each::

        {"report_id", "filename", "created_at", "summary", "clauses": [...]}
    """
    meta = []
    for d in drafts:
        summary = d.get("summary") or {}
        meta.append({
            "report_id": d["report_id"],
            "filename": d.get("filename") or "",
            "created_at": d.get("created_at") or "",
            "skor_kepatuhan": summary.get("skor_kepatuhan"),
            "skor_posisi": summary.get("skor_posisi"),
            "pihak_kita": summary.get("pihak_kita"),
            "peran_kita": summary.get("peran_kita"),
            "menguntungkan": summary.get("klausul_menguntungkan", 0),
            "merugikan": summary.get("klausul_merugikan", 0),
            "netral": summary.get("klausul_netral", 0),
        })

    # 1. Index every draft by label + occurrence, remembering source order.
    per_draft: list[dict[str, dict]] = []
    order: list[str] = []
    for d in drafts:
        indexed: dict[str, dict] = {}
        seen: dict[str, int] = {}
        for clause in d.get("clauses") or []:
            base = label_key(clause.get("pasal"))
            occ = seen.get(base, 0)
            seen[base] = occ + 1
            key = f"{base}#{occ}"
            indexed[key] = {
                "pasal": clause.get("pasal") or "Pasal",
                "text": clause.get("isi_pasal") or "",
                "cell": _cell(clause),
            }
            if key not in order:
                order.append(key)
        per_draft.append(indexed)

    # 2. Pair leftovers between consecutive drafts by wording; the later draft
    #    adopts the earlier draft's key so pass 1 above is enough to line them
    #    up in every following draft too.
    for i in range(1, len(drafts)):
        prev, cur = per_draft[i - 1], per_draft[i]
        left = [{"entry": k, "text": v["text"]} for k, v in cur.items() if k not in prev]
        right = [{"entry": k, "text": v["text"]} for k, v in prev.items() if k not in cur]
        for cur_key, prev_key in _pair_by_text(left, right):
            cur[prev_key] = cur.pop(cur_key)

    # 3. Build the rows.
    rows = []
    for key in order:
        cells = []
        pasal = ""
        for i, indexed in enumerate(per_draft):
            entry = indexed.get(key)
            if entry:
                pasal = pasal or entry["pasal"]
                cells.append({"report_id": meta[i]["report_id"], **entry["cell"]})
            else:
                cells.append({"report_id": meta[i]["report_id"], "present": False})

        direction = _direction(cells)
        if direction == "unchanged" and not any(c["present"] for c in cells):
            continue
        rows.append({
            "key": key,
            "pasal": pasal or key.split("#", 1)[0],
            "cells": cells,
            "change": direction,
            "status_change": _status_change(cells),
        })

    counts = {name: sum(1 for r in rows if r["change"] == name)
              for name in ("improved", "worsened", "added", "removed", "unchanged")}

    return {"drafts": meta, "clauses": rows, "summary": counts}

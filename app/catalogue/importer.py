"""Catalogue import (Advanced): CSV in -> validated, de-duplicated, reachability
checked, enriched, relevance-scored catalogue rows, plus a row-by-row report.

Duplicates are found on (normalised URL, platform), inside the file and
against the existing catalogue. 1,000 rows finish well under 5 minutes because
reachability and enrichment run concurrently (REACHABILITY_CONCURRENCY).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from langdetect import DetectorFactory, LangDetectException, detect

from app.catalogue import intel, store
from app.config import get_settings
from app.connectors.http import is_simulated
from app.db import get_db
from app.errors import ApiError
from app.rules import parse_window
from app.storage import get_storage

DetectorFactory.seed = 0

PLATFORM_TO_CONNECTOR = {
    "discourse": "discourse", "lemmy": "lemmy", "devto": "devto", "dev.to": "devto", "forem": "devto",
    "ghost": "ghost", "hashnode": "hashnode", "slack": "slack", "mrss": "mrss", "webhook": "webhook",
    "partner": "webhook", "partner_site": "webhook",
}
ASSISTED_PLATFORMS = {"reddit", "hackernews", "producthunt", "linkedin_group", "facebook_group", "indiehackers",
                      "quora", "discord", "medium", "substack", "peertube", "kbin", "mbin", "forum", "telegram_group",
                      "whatsapp_community", "slack_community", "newsletter", "betalist", "kit", "mailchimp"}
CONFIG_COLUMNS = ["base_url", "community", "channel", "feed_key", "publication_id", "submit_url", "submit_url_template",
                  "category_id", "api_base_url", "description"]
ISO_639_1 = set("""aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz
ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv
ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn
no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg
th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu""".split())


def _iso_lang(code: str) -> bool:
    base = code.lower().split("-")[0].split("_")[0]
    return base in ISO_639_1


def parse_csv(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ApiError("VALIDATION_ERROR", "the CSV has no header row")
    rows = []
    for raw in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k})
    return rows


def _valid_url(url: str) -> str | None:
    if not url:
        return "url is empty"
    if re.search(r"\s", url):
        return "url contains spaces"
    candidate = url if "://" in url else "https://" + url
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        return f"unsupported scheme '{parts.scheme}'"
    host = parts.hostname or ""
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host.lower()) or host.startswith(".") \
            or ".." in host:
        return f"'{url}' is not a valid web address"
    return None


def validate_row(raw: dict[str, str]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Returns (destination data or None, errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    url = raw.get("url") or raw.get("base_url") or ""
    err = _valid_url(url)
    if err:
        errors.append(err)
    platform = (raw.get("platform_key") or raw.get("platform") or "").strip().lower().replace(" ", "_")
    connector = (raw.get("connector_key") or "").strip().lower() or PLATFORM_TO_CONNECTOR.get(platform)
    if not platform and connector:
        platform = connector
    if not connector:
        if platform in ASSISTED_PLATFORMS or platform:
            connector = "assisted"
            if platform not in ASSISTED_PLATFORMS:
                warnings.append(f"unknown platform '{platform}': catalogued as assisted (a person posts)")
        else:
            errors.append("platform_key is missing")
    data: dict[str, Any] = {"url": url if "://" in url else f"https://{url}", "platform_key": platform,
                            "connector_key": connector, "name": raw.get("name") or None}
    for col in ("surface", "category", "geo", "rules_url", "link_policy", "relevance_reason"):
        if raw.get(col):
            data[col] = raw[col]
    tags = raw.get("audience_tags", "")
    data["audience_tags"] = [t.strip().lower() for t in re.split(r"[;,|]", tags) if t.strip()]
    if raw.get("language"):
        lang = raw["language"].strip()
        if _iso_lang(lang):
            data["language"] = lang.lower()
        else:
            warnings.append(f"language '{lang}' is not an ISO code; detected instead")
    if raw.get("est_audience"):
        try:
            data["est_audience"] = int(float(raw["est_audience"].replace(",", "")))
        except ValueError:
            warnings.append(f"est_audience '{raw['est_audience']}' is not a number; ignored")
    if raw.get("frequency_cap"):
        try:
            parse_window(raw["frequency_cap"])
            data["frequency_cap"] = raw["frequency_cap"]
        except ValueError as exc:
            errors.append(str(exc))
    if raw.get("self_promo_allowed"):
        sp = raw["self_promo_allowed"].lower()
        if sp in ("yes", "no", "limited") or sp.startswith("limited:"):
            data["self_promo_allowed"] = sp
        else:
            errors.append(f"self_promo_allowed must be yes, no or limited (got '{raw['self_promo_allowed']}')")
    for col in ("disclosure_required", "automation_allowed"):
        if raw.get(col):
            v = raw[col].lower()
            if v in ("1", "true", "yes", "y", "0", "false", "no", "n"):
                data[col] = v in ("1", "true", "yes", "y")
            else:
                errors.append(f"{col} must be true or false (got '{raw[col]}')")
    if connector == "assisted" and "automation_allowed" not in data:
        data["automation_allowed"] = False
    if raw.get("relevance_score"):
        try:
            rs = int(raw["relevance_score"])
            if not 1 <= rs <= 5:
                raise ValueError
            data["relevance_score"] = rs
        except ValueError:
            errors.append(f"relevance_score must be 1-5 (got '{raw['relevance_score']}')")
    cfg = {c: raw[c] for c in CONFIG_COLUMNS if raw.get(c)}
    if connector in ("discourse", "lemmy", "ghost", "webhook") and "base_url" not in cfg and not err:
        parts = urlsplit(data["url"])
        cfg["base_url"] = f"{parts.scheme}://{parts.netloc}"
    if connector == "lemmy" and "community" not in cfg:
        m = re.search(r"/c/([A-Za-z0-9_]+)", data["url"])
        if m:
            cfg["community"] = m.group(1)
        else:
            errors.append("lemmy rows need a community (column 'community' or a /c/<name> URL)")
    data["config"] = cfg
    return (None if errors else data), errors, warnings


async def check_reachability(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    s = get_settings()
    if s.reachability_mode == "offline":
        return {"reachable": None, "note": "not checked (offline mode)"}
    target, transport_client = url, client
    if is_simulated(url):
        return {"reachable": True, "http_status": 200, "note": "simulated host", "title": "", "description": ""}
    t0 = time.monotonic()
    try:
        resp = await transport_client.get(target)
        title, desc = "", ""
        if "html" in resp.headers.get("content-type", ""):
            head = resp.text[:60000]
            m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""
            m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', head, re.I)
            desc = m.group(1).strip()[:400] if m else ""
        return {"reachable": resp.status_code < 400, "http_status": resp.status_code, "final_url": str(resp.url),
                "ms": int((time.monotonic() - t0) * 1000), "title": title, "description": desc}
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": type(exc).__name__, "ms": int((time.monotonic() - t0) * 1000)}


async def enrich_members(client: httpx.AsyncClient, data: dict[str, Any]) -> int | None:
    """Member counts from public endpoints where the platform has one."""
    cfg = data["config"]
    if get_settings().reachability_mode == "offline" or is_simulated(cfg.get("base_url", data["url"])):
        return None
    try:
        if data["connector_key"] == "discourse":
            r = await client.get(cfg["base_url"].rstrip("/") + "/about.json")
            return int(r.json()["about"]["stats"]["users_count"])
        if data["connector_key"] == "lemmy":
            r = await client.get(cfg["base_url"].rstrip("/") + "/api/v3/community", params={"name": cfg["community"]})
            return int(r.json()["community_view"]["counts"]["subscribers"])
    except Exception:  # noqa: BLE001 - enrichment is best effort
        return None
    return None


def detect_language(text: str) -> str | None:
    text = re.sub(r"https?://\S+", " ", text or "")
    if len(text.strip()) < 12:
        return None
    try:
        return detect(text)
    except LangDetectException:
        return None


async def import_catalogue(data: bytes, *, source: str, progress=lambda p: None, score_with_ai: bool = False,
                           commit: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    rows = parse_csv(data)
    if len(rows) > 20000:
        raise ApiError("VALIDATION_ERROR", "at most 20,000 rows per import")
    s = get_settings()
    report: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    candidates: list[tuple[int, dict[str, Any], list[str]]] = []
    for i, raw in enumerate(rows, start=2):  # row 1 is the header
        data_row, errors, warnings = validate_row(raw)
        entry: dict[str, Any] = {"row": i, "url": raw.get("url", ""), "platform_key": raw.get("platform_key")
                                 or raw.get("platform", ""), "status": "", "errors": errors, "warnings": warnings}
        report.append(entry)
        if data_row is None:
            entry["status"] = "error"
            continue
        key = (store.normalize_url(data_row["url"]) + _instance_suffix(data_row), data_row["platform_key"])
        if key in seen:
            entry["status"] = "duplicate"
            entry["duplicate_of_row"] = seen[key]
            continue
        seen[key] = i
        existing = get_db().one("SELECT id FROM destinations WHERE normalized_url=? AND platform_key=?", list(key))
        if existing:
            entry["existing_id"] = existing["id"]
        candidates.append((len(report) - 1, data_row, warnings))
    progress(10)

    sem = asyncio.Semaphore(s.reachability_concurrency)
    done = {"n": 0}
    limits = httpx.Limits(max_connections=s.reachability_concurrency * 2, max_keepalive_connections=20)
    async with httpx.AsyncClient(timeout=s.reachability_timeout_s, follow_redirects=True, limits=limits,
                                 headers={"User-Agent": s.user_agent}) as client:

        async def work(idx: int, data_row: dict[str, Any], warnings: list[str]) -> None:
            async with sem:
                reach = await check_reachability(client, data_row["url"])
                members = await enrich_members(client, data_row) if reach.get("reachable") else None
            entry = report[idx]
            entry["reachability"] = {k: v for k, v in reach.items() if k not in ("title", "description")}
            if reach.get("reachable") is False:
                warnings.append(f"not reachable ({reach.get('http_status') or reach.get('error')})")
            if members is not None:
                data_row["est_audience"] = members
            desc = " ".join(x for x in (reach.get("title"), reach.get("description"),
                                        data_row["config"].get("description")) if x)
            if desc:
                data_row["config"]["description"] = desc[:400]
            detected = detect_language(" ".join(x for x in (data_row.get("name") or "", desc) if x))
            declared = data_row.get("language")
            if detected and declared and detected[:2] != declared[:2]:
                warnings.append(f"declared language '{declared}' but the page reads as '{detected}'")
            if not declared:
                data_row["language"] = detected or "en"
                entry["language_detected"] = detected
            if not data_row.get("relevance_score"):
                rel = await (intel.score_relevance(data_row) if score_with_ai else _heuristic(data_row))
                data_row["relevance_score"] = rel["score"]
                data_row["relevance_reason"] = rel["reason"]
                if not data_row.get("audience_tags"):
                    data_row["audience_tags"] = rel["audience_tags"]
            done["n"] += 1
            progress(10 + int(80 * done["n"] / max(1, len(candidates))))

        await asyncio.gather(*(work(idx, d, w) for idx, d, w in candidates))

    created = updated = 0
    for idx, data_row, warnings in candidates:
        entry = report[idx]
        entry["warnings"] = warnings
        if commit:
            row, was_created = store.upsert_destination(data_row)
            entry["destination_id"] = row["id"]
            created += int(was_created)
            updated += int(not was_created)
            entry["status"] = "created" if was_created else "updated"
        else:
            entry["status"] = "valid"
        entry["relevance_score"] = data_row.get("relevance_score")
        entry["relevance_reason"] = data_row.get("relevance_reason")
        entry["language"] = data_row.get("language")
        entry["est_audience"] = data_row.get("est_audience")
    progress(95)
    seconds = round(time.monotonic() - started, 2)
    summary = {
        "source": source, "rows": len(rows), "created": created, "updated": updated,
        "duplicates": sum(1 for r in report if r["status"] == "duplicate"),
        "errors": sum(1 for r in report if r["status"] == "error"),
        "with_warnings": sum(1 for r in report if r["warnings"]),
        "unreachable": sum(1 for r in report if (r.get("reachability") or {}).get("reachable") is False),
        "seconds": seconds, "rows_per_second": round(len(rows) / seconds, 1) if seconds else None,
    }
    storage = get_storage()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    links = {
        "report_csv": storage.put_text(f"imports/{stamp}/report.csv", _report_csv(report), "text/csv"),
        "report_json": storage.put_json(f"imports/{stamp}/report.json", {"summary": summary, "rows": report}),
        "catalogue_csv": storage.put_text(f"imports/{stamp}/catalogue.csv", export_catalogue_csv(), "text/csv"),
    }
    return {"summary": summary, "links": links, "rows_preview": report[:20]}


def _instance_suffix(data_row: dict[str, Any]) -> str:
    cfg = data_row.get("config") or {}
    norm = store.normalize_url(data_row["url"]).lower()
    suffix = ""
    for part in ("community", "channel", "feed_key", "publication_id", "category_id"):
        if cfg.get(part) and str(cfg[part]).lower() not in norm:
            suffix += f"#{part}={str(cfg[part]).lower()}"
    return suffix


async def _heuristic(data_row: dict[str, Any]) -> dict[str, Any]:
    return intel.heuristic_relevance(intel.describe(data_row))


def _report_csv(report: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row", "status", "url", "platform_key", "destination_id", "duplicate_of_row", "reachable", "http_status",
                "language", "est_audience", "relevance_score", "relevance_reason", "errors", "warnings"])
    for r in report:
        reach = r.get("reachability") or {}
        w.writerow([r["row"], r["status"], r["url"], r["platform_key"], r.get("destination_id", r.get("existing_id", "")),
                    r.get("duplicate_of_row", ""), reach.get("reachable", ""), reach.get("http_status", ""),
                    r.get("language", ""), r.get("est_audience", ""), r.get("relevance_score", ""),
                    r.get("relevance_reason", ""), " | ".join(r["errors"]), " | ".join(r["warnings"])])
    return buf.getvalue()


EXPORT_FIELDS = ["id", "name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo",
                 "est_audience", "rules_url", "frequency_cap", "self_promo_allowed", "link_policy",
                 "disclosure_required", "automation_allowed", "connector_key", "relevance_score", "relevance_reason",
                 "level", "last_certified_at", "status"]


def export_catalogue_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_FIELDS + ["config"])
    for row in store.list_destinations():
        vals = []
        for f in EXPORT_FIELDS:
            v = row.get(f)
            if f == "audience_tags":
                v = ";".join(v or [])
            elif f in ("disclosure_required", "automation_allowed"):
                v = "true" if v else "false"
            vals.append("" if v is None else v)
        w.writerow(vals + [json.dumps(row.get("config") or {}, ensure_ascii=False)])
    return buf.getvalue()

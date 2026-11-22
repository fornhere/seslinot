"""0-token deterministik kaynak kayıt defteri, URL kanonikleştirme ve SSRF korumalı indirici."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import trafilatura

from seslinot.config import SesliNotConfig
from seslinot.models import SourceRecord, SubagentOutput, VaultContextBundle, utc_now_iso

_TRACKING_PARAM_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "ref_url",
    }
)

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata",
    }
)

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

_BLOCKED_HTTP_STATUSES = frozenset({401, 402, 403, 406, 429, 451, 503})
_MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(slots=True)
class FetchResponse:
    """HTTP kaynak çekme yanıtı (gerçek httpx veya test mock'u için ortak biçim)."""

    status_code: int
    text: str
    final_url: str = ""
    content_type: str = "text/html"


FetchFunc = Callable[[str], Awaitable[FetchResponse | dict[str, Any] | tuple[int, str, str]]]


class _SSRFBlockedError(RuntimeError):
    """SSRF güvenlik denetimi bir URL'yi engellediğinde fırlatılır."""


def canonicalize_url(url: str) -> str:
    """URL'yi izleme parametrelerinden, fragment'tan ve biçim farklarından arındırır."""
    raw = url.strip().rstrip(".,;:!?)\"'`>")
    if not raw:
        return ""

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if not scheme or not parsed.hostname:
        return raw

    hostname = parsed.hostname.lower().rstrip(".")
    if ":" in hostname:  # IPv6 literal
        host_part = f"[{hostname}]"
    else:
        host_part = hostname

    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        userinfo += "@"

    netloc = f"{userinfo}{host_part}" + (f":{port}" if port is not None else "")

    path = re.sub(r"/{2,}", "/", parsed.path)
    path = path.rstrip("/")

    kept_params: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in _TRACKING_PARAM_EXACT:
            continue
        kept_params.append((k, v))
    kept_params.sort(key=lambda item: (item[0].lower(), item[1]))
    query = urlencode(kept_params, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def _check_ip_safety(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
    # IPv4-mapped IPv6 adreslerini (ör. ::ffff:127.0.0.1) asıl IPv4 adresine indirge
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped

    if ip_obj.is_loopback:
        return False, f"Loopback IP engellendi: {ip_obj}"
    if ip_obj.is_private:
        return False, f"Özel ağ IP'si engellendi: {ip_obj}"
    if ip_obj.is_link_local:
        return False, f"Link-local IP engellendi: {ip_obj}"
    if ip_obj.is_unspecified:
        return False, f"Belirtilmemiş IP engellendi: {ip_obj}"
    if ip_obj.is_multicast:
        return False, f"Multicast IP engellendi: {ip_obj}"
    if ip_obj.is_reserved:
        return False, f"Rezerve IP engellendi: {ip_obj}"
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NET:
        return False, f"CGNAT IP engellendi: {ip_obj}"
    return True, "ok"


def is_ssrf_safe_url(url: str, *, resolve_dns: bool = True) -> tuple[bool, str]:
    """URL'nin SSRF açısından güvenli (genel http/https) olup olmadığını doğrular."""
    if not url or not url.strip():
        return False, "Boş URL"

    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        return False, f"Geçersiz URL: {exc}"

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return False, f"İzin verilmeyen protokol şeması: {scheme or 'yok'}"

    hostname = parsed.hostname
    if not hostname:
        return False, "Ana makine adı eksik"

    host = hostname.lower().rstrip(".")
    if (
        host in _BLOCKED_HOSTNAMES
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        return False, f"Yerel ana makine engellendi: {host}"

    # Doğrudan IP literali mi?
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        return _check_ip_safety(ip_obj)

    if not resolve_dns:
        return True, "ok"

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        return False, f"DNS çözümlenemedi ({host}): {exc}"

    if not addr_infos:
        return False, f"DNS kaydı bulunamadı: {host}"

    for info in addr_infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved_ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False, f"Geçersiz DNS IP yanıtı: {raw_ip}"
        safe, reason = _check_ip_safety(resolved_ip)
        if not safe:
            return False, f"DNS üzerinden {reason}"

    return True, "ok"


def extract_clean_text(raw_body: str, content_type: str = "") -> tuple[str, str]:
    """HTML veya düz metin gövdesinden temiz metin ve opsiyonel HTML başlığı çıkarır."""
    if not raw_body or not raw_body.strip():
        return "", ""

    ct = content_type.lower()
    looks_html = (
        "html" in ct
        or "xml" in ct
        or bool(re.search(r"<(?:!doctype|html|head|body|article|main|p|div)\b", raw_body[:2048], re.IGNORECASE))
    )

    if not looks_html:
        return raw_body.strip(), ""

    title = ""
    m_title = _TITLE_RE.search(raw_body)
    if m_title:
        title = re.sub(r"\s+", " ", html.unescape(m_title.group(1))).strip()

    extracted = ""
    try:
        res = trafilatura.extract(
            raw_body,
            include_comments=False,
            include_tables=True,
            favor_precision=False,
        )
        if isinstance(res, str):
            extracted = res.strip()
    except Exception:
        extracted = ""

    if len(extracted) < 40:
        stripped = _SCRIPT_STYLE_RE.sub(" ", raw_body)
        stripped = _TAG_RE.sub(" ", stripped)
        stripped = html.unescape(stripped)
        fallback_text = re.sub(r"\s+", " ", stripped).strip()
        if len(fallback_text) > len(extracted):
            extracted = fallback_text

    return extracted, title


def _normalize_fetch_response(
    raw: FetchResponse | dict[str, Any] | tuple[int, str, str],
    default_url: str,
) -> FetchResponse:
    if isinstance(raw, FetchResponse):
        if not raw.final_url:
            raw.final_url = default_url
        return raw
    if isinstance(raw, dict):
        return FetchResponse(
            status_code=int(raw.get("status_code", 200)),
            text=str(raw.get("text", "")),
            final_url=str(raw.get("final_url") or default_url),
            content_type=str(raw.get("content_type") or "text/html"),
        )
    status_code, text, final_url = raw
    return FetchResponse(
        status_code=int(status_code),
        text=str(text),
        final_url=str(final_url or default_url),
        content_type="text/html",
    )


class SourceRegistry:
    """0-token deterministik kaynak kayıt, tekilleştirme ve anlık görüntü deposu."""

    def __init__(
        self,
        session_dir: Path,
        vault_path: Path,
        cfg: SesliNotConfig,
        *,
        fetcher: FetchFunc | None = None,
        ssrf_checker: Callable[[str], tuple[bool, str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.vault_path = Path(vault_path)
        self.cfg = cfg
        self._fetcher = fetcher
        self._transport = transport
        if ssrf_checker is not None:
            self._ssrf_checker = ssrf_checker
        elif fetcher is not None or transport is not None:
            # Özel test fetcher/transport verildiyse şema/IP/localhost SSRF kurallarını koru,
            # ancak çevrimdışı test alan adlarında gerçek DNS sorgusu yapma.
            self._ssrf_checker = lambda u: is_ssrf_safe_url(u, resolve_dns=False)
        else:
            self._ssrf_checker = lambda u: is_ssrf_safe_url(u, resolve_dns=True)

        self.sources_dir = self.session_dir / "sources"
        self.snapshots_dir = self.sources_dir / "snapshots"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        self.records: dict[str, SourceRecord] = {}
        self._canon_to_id: dict[str, str] = {}
        self._vault_path_to_id: dict[str, str] = {}
        self._next_s_num = 1
        self._next_v_num = 1

    def _alloc_s_id(self) -> str:
        while True:
            sid = f"S{self._next_s_num:02d}"
            self._next_s_num += 1
            if sid not in self.records:
                return sid

    def _alloc_v_id(self) -> str:
        while True:
            vid = f"V{self._next_v_num:02d}"
            self._next_v_num += 1
            if vid not in self.records:
                return vid

    def _resolve_safe_vault_file(self, rel_path: str) -> Path | None:
        if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
            return None
        try:
            vault_root = self.vault_path.resolve()
            candidate = (vault_root / rel_path).resolve()
            if not candidate.is_relative_to(vault_root):
                return None
            return candidate
        except OSError:
            return None

    def ingest_vault_context(self, bundle: VaultContextBundle | None) -> list[SourceRecord]:
        """VaultContextBundle içindeki kasa eşleşmelerini V01..Vn olarak kaydeder."""
        if bundle is None:
            return []

        created: list[SourceRecord] = []
        for hit in bundle.hits:
            norm_path = hit.path.strip().lstrip("/")
            if not norm_path:
                continue
            if norm_path in self._vault_path_to_id:
                existing = self.records[self._vault_path_to_id[norm_path]]
                created.append(existing)
                continue

            preferred_id = hit.source_id.strip() if re.fullmatch(r"V\d+", hit.source_id.strip()) else ""
            if preferred_id and preferred_id not in self.records:
                vid = preferred_id
                num = int(preferred_id[1:])
                self._next_v_num = max(self._next_v_num, num + 1)
            else:
                vid = self._alloc_v_id()

            resolved = self._resolve_safe_vault_file(norm_path)
            exists = resolved is not None and resolved.is_file()
            snapshot_path: str | None = None
            snapshot_sha256: str | None = None
            if exists and resolved is not None:
                try:
                    content = resolved.read_text(encoding="utf-8", errors="replace")
                    snap_file = self.snapshots_dir / f"{vid}.txt"
                    snap_file.write_text(content, encoding="utf-8")
                    snapshot_path = str(snap_file)
                    snapshot_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                except OSError:
                    exists = False

            rec = SourceRecord(
                source_id=vid,
                source_type="vault",
                vault_path=norm_path,
                url_hash=hashlib.sha256(norm_path.encode("utf-8")).hexdigest()[:16],
                title=hit.title or Path(norm_path).stem,
                fetch_status="vault_ok" if exists else "vault_missing",
                seen_in_tool_trace=True,
                snapshot_path=snapshot_path,
                snapshot_sha256=snapshot_sha256,
                retrieved_at=utc_now_iso(),
                is_primary=True,
            )
            self.records[vid] = rec
            self._vault_path_to_id[norm_path] = vid
            created.append(rec)

        return created

    def ingest_subagent_outputs(
        self,
        outputs: list[SubagentOutput],
        observed_urls_by_worker: dict[str, list[str]] | None = None,
    ) -> dict[tuple[str, str], str]:
        """Subagent bulgularındaki kaynakları tekilleştirerek S01..Sn / Vxx olarak kaydeder."""
        observed_all_canon: set[str] = set()
        observed_all_raw: set[str] = set()
        if observed_urls_by_worker:
            for url_list in observed_urls_by_worker.values():
                for raw_u in url_list:
                    observed_all_raw.add(raw_u.strip())
                    c_u = canonicalize_url(raw_u)
                    if c_u:
                        observed_all_canon.add(c_u)

        finding_map: dict[tuple[str, str], str] = {}

        for out in outputs:
            worker_id = out.worker_id
            for finding in out.findings:
                key = (worker_id, finding.claim_id)
                if finding.source_type == "vault" and finding.vault_path:
                    norm_path = finding.vault_path.strip().lstrip("/")
                    if norm_path in self._vault_path_to_id:
                        vid = self._vault_path_to_id[norm_path]
                        rec = self.records[vid]
                    else:
                        vid = self._alloc_v_id()
                        resolved = self._resolve_safe_vault_file(norm_path)
                        exists = resolved is not None and resolved.is_file()
                        snap_path: str | None = None
                        snap_sha: str | None = None
                        if exists and resolved is not None:
                            content = resolved.read_text(encoding="utf-8", errors="replace")
                            snap_file = self.snapshots_dir / f"{vid}.txt"
                            snap_file.write_text(content, encoding="utf-8")
                            snap_path = str(snap_file)
                            snap_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                        rec = SourceRecord(
                            source_id=vid,
                            source_type="vault",
                            vault_path=norm_path,
                            url_hash=hashlib.sha256(norm_path.encode("utf-8")).hexdigest()[:16],
                            title=finding.title or Path(norm_path).stem,
                            fetch_status="vault_ok" if exists else "vault_missing",
                            seen_in_tool_trace=True,
                            snapshot_path=snap_path,
                            snapshot_sha256=snap_sha,
                            retrieved_at=finding.retrieved_at or utc_now_iso(),
                            is_primary=True,
                        )
                        self.records[vid] = rec
                        self._vault_path_to_id[norm_path] = vid

                    if worker_id not in rec.referenced_by_workers:
                        rec.referenced_by_workers.append(worker_id)
                    finding_map[key] = vid
                    continue

                raw_url = (finding.url or "").strip()
                if not raw_url:
                    finding_map[key] = ""
                    continue

                canon = canonicalize_url(raw_url)
                seen_in_trace = (canon in observed_all_canon) or (raw_url in observed_all_raw)

                if canon in self._canon_to_id:
                    sid = self._canon_to_id[canon]
                    rec = self.records[sid]
                    if raw_url not in rec.original_urls:
                        rec.original_urls.append(raw_url)
                    if worker_id not in rec.referenced_by_workers:
                        rec.referenced_by_workers.append(worker_id)
                    if not rec.title and finding.title:
                        rec.title = finding.title
                    if not rec.published_at and finding.published_at:
                        rec.published_at = finding.published_at
                    if seen_in_trace:
                        rec.seen_in_tool_trace = True
                else:
                    sid = self._alloc_s_id()
                    rec = SourceRecord(
                        source_id=sid,
                        source_type="web",
                        original_urls=[raw_url],
                        canonical_url=canon,
                        final_url=canon,
                        url_hash=hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16],
                        title=finding.title,
                        published_at=finding.published_at,
                        fetch_status="dead_url",  # fetch_all() doğrulayana kadar fail-closed
                        seen_in_tool_trace=seen_in_trace,
                        retrieved_at=finding.retrieved_at or utc_now_iso(),
                        referenced_by_workers=[worker_id],
                    )
                    self.records[sid] = rec
                    self._canon_to_id[canon] = sid

                finding_map[key] = sid

        return finding_map

    async def _fetch_single_web(
        self,
        rec: SourceRecord,
        sem: asyncio.Semaphore,
        client: httpx.AsyncClient | None,
    ) -> None:
        async with sem:
            target_url = rec.canonical_url
            safe, _reason = await asyncio.to_thread(self._ssrf_checker, target_url)
            if not safe:
                rec.fetch_status = "dead_url"
                rec.http_status = None
                return

            try:
                if self._fetcher is not None:
                    raw_resp = await self._fetcher(target_url)
                    resp = _normalize_fetch_response(raw_resp, target_url)
                else:
                    assert client is not None
                    http_resp = await client.get(target_url)
                    body_bytes = http_resp.content[:_MAX_BODY_BYTES]
                    encoding = http_resp.encoding or "utf-8"
                    resp = FetchResponse(
                        status_code=http_resp.status_code,
                        text=body_bytes.decode(encoding, errors="replace"),
                        final_url=str(http_resp.url),
                        content_type=http_resp.headers.get("content-type", "text/html"),
                    )
            except (_SSRFBlockedError, httpx.HTTPError, OSError, TimeoutError):
                rec.fetch_status = "dead_url"
                rec.http_status = None
                return
            except Exception:
                rec.fetch_status = "dead_url"
                rec.http_status = None
                return

            # Yönlendirilmiş son URL'yi de SSRF açısından doğrula
            final_url = resp.final_url or target_url
            final_safe, _final_reason = await asyncio.to_thread(self._ssrf_checker, final_url)
            if not final_safe:
                rec.fetch_status = "dead_url"
                rec.http_status = None
                return

            rec.http_status = resp.status_code
            rec.final_url = canonicalize_url(final_url) or final_url
            rec.retrieved_at = utc_now_iso()

            if 200 <= resp.status_code < 300:
                clean_text, html_title = extract_clean_text(resp.text, resp.content_type)
                if not rec.title and html_title:
                    rec.title = html_title
                if clean_text.strip():
                    snap_file = self.snapshots_dir / f"{rec.source_id}.txt"
                    snap_file.write_text(clean_text, encoding="utf-8")
                    rec.snapshot_path = str(snap_file)
                    rec.snapshot_sha256 = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
                    rec.fetch_status = "fetched_ok"
                elif rec.seen_in_tool_trace:
                    rec.fetch_status = "fetch_blocked_snippet_only"
                else:
                    rec.fetch_status = "dead_url"
            elif resp.status_code in _BLOCKED_HTTP_STATUSES and rec.seen_in_tool_trace:
                rec.fetch_status = "fetch_blocked_snippet_only"
            else:
                rec.fetch_status = "dead_url"

    async def fetch_all(self) -> list[SourceRecord]:
        """Tüm web kaynaklarını eşzamanlılık sınırı ve SSRF korumasıyla indirip doğrular."""
        web_records = [r for r in self.records.values() if r.source_type == "web"]
        sem = asyncio.Semaphore(max(1, self.cfg.research.fetch_concurrency))

        if web_records:
            if self._fetcher is not None:
                await asyncio.gather(
                    *(self._fetch_single_web(r, sem, None) for r in web_records)
                )
            else:
                async def _ssrf_request_hook(request: httpx.Request) -> None:
                    safe, reason = await asyncio.to_thread(self._ssrf_checker, str(request.url))
                    if not safe:
                        raise _SSRFBlockedError(reason)

                timeout = httpx.Timeout(self.cfg.research.fetch_timeout_secs)
                headers = {
                    "User-Agent": "SesliNot/0.1 (+local-research-verifier)",
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                }
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    max_redirects=5,
                    timeout=timeout,
                    headers=headers,
                    event_hooks={"request": [_ssrf_request_hook]},
                    transport=self._transport,
                ) as client:
                    await asyncio.gather(
                        *(self._fetch_single_web(r, sem, client) for r in web_records)
                    )

        self.save()
        return self.ordered_records()

    def ordered_records(self) -> list[SourceRecord]:
        """Önce Vxx sonra Sxx olacak şekilde sıralı SourceRecord listesi döndürür."""
        def _sort_key(rec: SourceRecord) -> tuple[int, int, str]:
            prefix = rec.source_id[:1]
            try:
                num = int(rec.source_id[1:])
            except ValueError:
                num = 999999
            return (0 if prefix == "V" else 1, num, rec.source_id)

        return sorted(self.records.values(), key=_sort_key)

    def get_snapshot_text(self, source_id: str) -> str:
        """Verilen kaynağın kaydedilmiş anlık görüntü veya kasa metnini döndürür."""
        rec = self.records.get(source_id)
        if rec is None:
            return ""
        if rec.snapshot_path:
            snap = Path(rec.snapshot_path)
            if snap.is_file():
                try:
                    return snap.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
        if rec.source_type == "vault" and rec.vault_path:
            resolved = self._resolve_safe_vault_file(rec.vault_path)
            if resolved is not None and resolved.is_file():
                try:
                    return resolved.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
        return ""

    def save(self) -> Path:
        """Kanonik sources/sources.jsonl dosyasını atomik olarak yazar."""
        out_path = self.sources_dir / "sources.jsonl"
        lines = [rec.model_dump_json() for rec in self.ordered_records()]
        tmp_path = self.sources_dir / "sources.jsonl.tmp"
        tmp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp_path.replace(out_path)
        return out_path

    @classmethod
    def load_records(cls, session_dir: Path) -> dict[str, SourceRecord]:
        """Diskteki sources/sources.jsonl dosyasından kayıtları yükler."""
        path = Path(session_dir) / "sources" / "sources.jsonl"
        if not path.is_file():
            return {}
        loaded: dict[str, SourceRecord] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            rec = SourceRecord.model_validate_json(stripped)
            loaded[rec.source_id] = rec
        return loaded

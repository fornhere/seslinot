"""Hafıza OS köprüsü: salt-okunur bağlam toplama, gizlilik duvarı ve onaylı terfi."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Literal

from seslinot.config import SesliNotConfig
from seslinot.models import VaultContextBundle, VaultContextHit
from seslinot.platform.os_compat import default_user_name, is_hafiza_os_vault
from seslinot.vault.index import VaultIndex
from seslinot.vault.writer import (
    SECRET_RULES,
    _normalize_rel_posix,
    contains_secret,
    turkish_lower,
    turkish_slug,
)

_GENERIC_SUBJECT_TERMS: frozenset[str] = frozenset(
    {"kullanıcı", "kullanici", "user", "kişi", "kisi", "ben"}
)
_PUBLIC_PLATFORM_IDS: frozenset[str] = frozenset(
    {"youtube", "twitter", "github", "obsidian", "linux", "wayland", "pipewire"}
)

_UNIX_PATH_RE = re.compile(
    r"(?:~|/(?:home|Users|root|tmp|var|etc|opt|mnt|media|srv|usr|run))(?:/[^\s\"'<>|,;)]+)*"
)
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>|,;)]+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PRIVATE_IP_RE = re.compile(
    r"\b(?:127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class HafizaBridge:
    """Hafıza OS `araclar/hafiza.py` ve `VaultIndex` birleşik köprü katmanı (opsiyonel eklenti)."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        index: VaultIndex | None = None,
        timeout_secs: float = 15.0,
    ) -> None:
        self.cfg = cfg
        self.vault_path = Path(cfg.vault.path).expanduser().resolve()
        self.timeout_secs = timeout_secs
        self.index = index or VaultIndex(self.vault_path, cfg.paths.fts_db_path)

    @property
    def hafiza_os_enabled(self) -> bool:
        mode = getattr(self.cfg.vault, "hafiza_os_mode", "auto")
        if mode == "disabled":
            return False
        if mode == "enabled":
            return True
        return is_hafiza_os_vault(self.vault_path)

    @property
    def hafiza_script(self) -> Path:
        return self.vault_path / self.cfg.vault.hafiza_tools_dir / "hafiza.py"

    def _has_hafiza_script(self) -> bool:
        if not self.hafiza_os_enabled:
            return False
        script = self.hafiza_script
        if not script.is_file() or script.is_symlink():
            return False
        try:
            return script.resolve().is_relative_to(self.vault_path)
        except OSError:
            return False

    def _run_hafiza_cli(
        self,
        args: list[str],
        *,
        accept_rc1: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """`araclar/hafiza.py` aracını `HAFIZA_HOOK_JEV=0` ile güvenli çalıştırır."""
        if not self._has_hafiza_script():
            return -1, {}

        cmd = [
            sys.executable,
            "-X",
            "utf8",
            str(self.hafiza_script),
            "--vault",
            str(self.vault_path),
            *args,
        ]
        env = dict(os.environ)
        env["HAFIZA_HOOK_JEV"] = "0"
        env["PYTHONUTF8"] = "1"

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.vault_path),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_secs,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1, {}

        ok_codes = (0, 1) if accept_rc1 else (0,)
        stdout = (proc.stdout or "").strip()
        if proc.returncode not in ok_codes:
            if stdout.startswith("{"):
                try:
                    parsed = json.loads(stdout)
                    if isinstance(parsed, dict):
                        return proc.returncode, parsed
                except json.JSONDecodeError:
                    pass
            return proc.returncode, {"error": (proc.stderr or stdout).strip()}

        if not stdout:
            return proc.returncode, {}
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return proc.returncode, {}
        return proc.returncode, data if isinstance(data, dict) else {}

    @staticmethod
    def _note_dict_to_hit(
        note: dict[str, Any],
        *,
        default_kind: Literal["catalog", "bilgi", "note", "concept"],
    ) -> VaultContextHit | None:
        raw_path = note.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        rel_path = _normalize_rel_posix(raw_path)
        excerpt = _nfc(str(note.get("excerpt", "")).strip())
        anchor = _nfc(str(note.get("anchor", "")).strip())
        title = _nfc(str(note.get("title") or Path(rel_path).stem))
        try:
            score = float(note.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        kind: Literal["catalog", "bilgi", "note", "concept"] = default_kind
        if rel_path.startswith("bilgi/"):
            kind = "bilgi"
        elif rel_path.startswith("beyin/"):
            kind = "concept"
        return VaultContextHit(
            source_id="V00",
            path=rel_path,
            title=title,
            anchor=anchor,
            excerpt=excerpt,
            score=round(score, 4),
            kind=kind,
        )

    def gather_vault_context(
        self,
        query: str,
        *,
        limit: int = 6,
    ) -> VaultContextBundle:
        """Kasa bağlamını kavram eşleme + salt-okunur `hafiza.py` + FTS5 ile birleştirir."""
        clean_query = _nfc(query.strip())
        scope, matched_concepts, matched_projects, expanded_terms = (
            self.index.match_concepts_and_projects(clean_query)
        )

        catalog_text = ""
        collected: list[VaultContextHit] = []
        seen_paths: set[str] = set()

        def _append_unique(hit: VaultContextHit) -> None:
            key = _nfc(hit.path).casefold()
            if key in seen_paths:
                return
            seen_paths.add(key)
            collected.append(hit)

        # 2. Varsa `araclar/hafiza.py` salt-okunur 1A (katalog + bilgi/) ve 1B (--uri) çağrıları
        if clean_query and self._has_hafiza_script():
            base_args = [
                "context",
                clean_query,
                "--local",
                "--scope",
                scope,
                "--limit",
                str(limit),
                "--char-budget",
                "1800",
            ]
            # 1A: Kanonik katalog + incelenmiş bilgi/
            _, res_1a = self._run_hafiza_cli(base_args, accept_rc1=True)
            text_1a = res_1a.get("text")
            if isinstance(text_1a, str) and text_1a.strip():
                catalog_text = _nfc(text_1a.strip())
            notes_1a = res_1a.get("notes")
            if isinstance(notes_1a, list):
                for item in notes_1a:
                    if isinstance(item, dict):
                        hit = self._note_dict_to_hit(item, default_kind="bilgi")
                        if hit is not None:
                            _append_unique(hit)

            # 1B: Seçili kasa dizinleri
            uri_dirs = list(
                dict.fromkeys(
                    [
                        self.cfg.vault.research_dir,
                        self.cfg.vault.voice_dir,
                        "projeler",
                        "görüşler",
                        "tavsiyeler",
                    ]
                )
            )
            uri_args = list(base_args)
            for udir in uri_dirs:
                uri_args.extend(["--uri", udir])
            _, res_1b = self._run_hafiza_cli(uri_args, accept_rc1=True)
            if not catalog_text:
                text_1b = res_1b.get("text")
                if isinstance(text_1b, str) and text_1b.strip():
                    catalog_text = _nfc(text_1b.strip())
            notes_1b = res_1b.get("notes")
            if isinstance(notes_1b, list):
                for item in notes_1b:
                    if isinstance(item, dict):
                        hit = self._note_dict_to_hit(item, default_kind="note")
                        if hit is not None:
                            _append_unique(hit)

        # 3. Artımlı FTS5 senkronizasyonu ve arama
        self.index.sync()
        if clean_query:
            enriched_query = clean_query
            if expanded_terms:
                enriched_query = f"{clean_query} {' '.join(expanded_terms[:4])}"
            fts_hits = self.index.search(enriched_query, limit=max(limit, 8))
            for hit in fts_hits:
                _append_unique(hit)

        final_hits: list[VaultContextHit] = []
        for idx, hit in enumerate(collected[: max(0, limit)], start=1):
            final_hits.append(hit.model_copy(update={"source_id": f"V{idx:02d}"}))

        return VaultContextBundle(
            query=clean_query,
            scope=scope,
            matched_concepts=matched_concepts,
            matched_projects=matched_projects,
            expanded_terms=expanded_terms,
            catalog_text=catalog_text,
            hits=final_hits,
        )

    def _collect_private_terms(self, extra_terms: list[str] | None = None) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()

        def _add(term: str) -> None:
            clean = _nfc(term.strip())
            key = turkish_lower(clean)
            if len(clean) < 2 or key in seen or key in _GENERIC_SUBJECT_TERMS:
                return
            seen.add(key)
            terms.append(clean)

        # Yapılandırmadaki kişisel terimler ve yerel OS kullanıcı adı (otomatik mahremiyet)
        for item in getattr(self.cfg.vault, "personal_terms", ()):
            if isinstance(item, str):
                _add(item)
        os_user = default_user_name()
        if os_user and len(os_user) >= 3 and os_user.lower() not in {"user", "root", "admin", "runner"}:
            _add(os_user)

        # komuta/kavramlar.json -> ozne listesi
        kavram_path = self.vault_path / "komuta" / "kavramlar.json"
        if kavram_path.is_file() and not kavram_path.is_symlink():
            try:
                kdata = json.loads(kavram_path.read_text(encoding="utf-8"))
                ozne = kdata.get("ozne") if isinstance(kdata, dict) else None
                if isinstance(ozne, list):
                    for item in ozne:
                        if isinstance(item, str):
                            _add(item)
            except (OSError, json.JSONDecodeError):
                pass

        # komuta/yayin-yasak-terimler.txt
        forbidden_path = self.vault_path / "komuta" / "yayin-yasak-terimler.txt"
        if forbidden_path.is_file() and not forbidden_path.is_symlink():
            try:
                for line in forbidden_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        _add(stripped)
            except OSError:
                pass

        # komuta/gorev-baglam.json -> aktif özel proje kimlikleri
        baglam_path = self.vault_path / "komuta" / "gorev-baglam.json"
        if baglam_path.is_file() and not baglam_path.is_symlink():
            try:
                bdata = json.loads(baglam_path.read_text(encoding="utf-8"))
                projects = bdata.get("projects") if isinstance(bdata, dict) else None
                if isinstance(projects, list):
                    for proj in projects:
                        if not isinstance(proj, dict):
                            continue
                        status = str(proj.get("status", "active")).casefold()
                        kind = str(proj.get("kind", "project")).casefold()
                        pid = proj.get("id")
                        if not isinstance(pid, str) or not pid.strip():
                            continue
                        if status == "archived" or kind == "area":
                            continue
                        if pid.casefold() in _PUBLIC_PLATFORM_IDS:
                            continue
                        _add(pid)
                        if "-" in pid:
                            _add(pid.replace("-", " "))
            except (OSError, json.JSONDecodeError):
                pass

        if extra_terms:
            for item in extra_terms:
                if isinstance(item, str):
                    _add(item)

        # Uzun terimler önce temizlensin
        terms.sort(key=len, reverse=True)
        return terms

    def sanitize_web_query(
        self,
        query: str,
        *,
        private_terms: list[str] | None = None,
    ) -> str:
        """Gizlilik Duvarı: dış web sorgularından kişi adları, yerel yollar, sırlar ve özel proje kimliklerini temizler."""
        if not query or not query.strip():
            return ""

        cleaned = _nfc(query)

        # 1. Ham sır kalıplarını tamamen kaldır (dış web'e maske etiketi bile gönderme)
        for _, pattern in SECRET_RULES:
            cleaned = pattern.sub(" ", cleaned)

        # 2. Yerel dosya yolları, e-postalar ve özel IP adresleri
        cleaned = _UNIX_PATH_RE.sub(" ", cleaned)
        cleaned = _WIN_PATH_RE.sub(" ", cleaned)
        cleaned = _EMAIL_RE.sub(" ", cleaned)
        cleaned = _PRIVATE_IP_RE.sub(" ", cleaned)

        # 3. Kişi adları + özel proje kimlikleri + kullanıcı tarafından verilen terimler
        all_private = self._collect_private_terms(private_terms)
        for term in all_private:
            escaped = re.escape(term)
            # Kelime sınırı + Türkçe kesme işaretli ekleri de temizle (örn. Forn'un -> "")
            pattern = re.compile(
                rf"(?<!\w){escaped}(?:['’][^\W_]+)?(?!\w)",
                re.IGNORECASE | re.UNICODE,
            )
            cleaned = pattern.sub(" ", cleaned)

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = cleaned.strip(" ,;:-–—/|")
        return _nfc(cleaned)

    def promote_user_approved_memory(
        self,
        *,
        statement: str,
        subject_key: str,
        source_path: str,
        source_anchor: str = "ham-döküm",
        scope: str = "user",
        kind: str = "semantic",
        memory_id: str | None = None,
        reviewed_by: str | None = None,
        proposed_by: str = "seslinot",
        evidence: str | None = None,
        category: str | None = None,
        search_keys: list[str] | None = None,
        supersedes: str | None = None,
        user_approved: bool = True,
    ) -> dict[str, Any]:
        """Kullanıcı onaylı açık hafıza terfisi (`candidate-add` -> `candidate-assess` -> `promote`).

        ASLA otomatik çağrılmaz; `user_approved=True` açıkça doğrulanır ve gizli veri
        barındıran beyanlar fail-closed reddedilir.
        """
        if not user_approved:
            raise ValueError("Kullanıcı onayı olmadan Hafıza OS terfisi yapılamaz")

        reviewer = (reviewed_by or default_user_name()).strip() or "user"

        clean_statement = _nfc(statement.strip())
        if not clean_statement:
            raise ValueError("Terfi edilecek beyan boş olamaz")
        if contains_secret(clean_statement) or (evidence and contains_secret(evidence)):
            raise ValueError("Gizli bilgi içeren beyan hafızaya terfi edilemez")

        if not self._has_hafiza_script():
            return {
                "ok": False,
                "promoted": False,
                "stage": "preflight",
                "error": "araclar/hafiza.py bulunamadı",
            }

        # 1. candidate-add
        add_args = [
            "candidate-add",
            "--statement",
            clean_statement,
            "--kind",
            kind,
            "--scope",
            scope,
            "--subject-key",
            subject_key,
            "--source-path",
            _normalize_rel_posix(source_path),
            "--source-anchor",
            source_anchor,
            "--proposed-by",
            proposed_by,
        ]
        if category:
            add_args.extend(["--category", category])
        if evidence:
            add_args.extend(["--evidence", evidence])

        rc_add, add_res = self._run_hafiza_cli(add_args, accept_rc1=False)
        candidate_id = add_res.get("candidate_id") if isinstance(add_res, dict) else None
        if rc_add != 0 or not isinstance(candidate_id, str) or not candidate_id:
            return {
                "ok": False,
                "promoted": False,
                "stage": "candidate-add",
                "response": add_res,
            }

        # 2. candidate-assess
        rc_assess, assess_res = self._run_hafiza_cli(
            ["candidate-assess", candidate_id],
            accept_rc1=False,
        )
        verdict = assess_res.get("result") if isinstance(assess_res, dict) else None
        can_promote = verdict == "eligible" or (verdict == "conflict" and bool(supersedes))
        if rc_assess != 0 or not can_promote:
            return {
                "ok": False,
                "promoted": False,
                "stage": "candidate-assess",
                "candidate_id": candidate_id,
                "assessment": assess_res,
            }

        # 3. promote --reviewed-by <reviewer> --apply
        final_memory_id = memory_id or (
            f"seslinot-{turkish_slug(subject_key, max_words=4)}-{secrets.token_hex(3)}"
        )
        promo_args = [
            "promote",
            candidate_id,
            "--memory-id",
            final_memory_id,
            "--reviewed-by",
            reviewer,
            "--apply",
        ]
        if supersedes:
            promo_args.extend(["--supersedes", supersedes])
        if search_keys:
            for key in search_keys:
                if key.strip():
                    promo_args.extend(["--search-key", key.strip()])

        rc_promo, promo_res = self._run_hafiza_cli(promo_args, accept_rc1=False)
        if rc_promo != 0:
            return {
                "ok": False,
                "promoted": False,
                "stage": "promote",
                "candidate_id": candidate_id,
                "memory_id": final_memory_id,
                "assessment": assess_res,
                "promotion": promo_res,
            }

        return {
            "ok": True,
            "promoted": True,
            "stage": "promote",
            "candidate_id": candidate_id,
            "memory_id": final_memory_id,
            "assessment": assess_res,
            "promotion": promo_res,
        }

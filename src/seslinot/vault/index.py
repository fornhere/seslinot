"""Kasa dışı artımlı SQLite FTS5 arama, kavram/proje eşleme ve wikilink keşfi."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from seslinot.models import VaultContextHit
from seslinot.vault.writer import (
    _FRONTMATTER_RE,
    _extract_h1_title,
    _is_forbidden_link_target,
    _normalize_rel_posix,
    _parse_flat_frontmatter,
    _strip_md,
    contains_secret,
    turkish_lower,
)

_EXCLUDED_DIR_NAMES = (
    ".git",
    ".obsidian",
    ".trash",
    ".cache",
    ".claude",
    "gelen-kutusu",
    "günlük",
    "arşiv",
    "araclar",
    "beyin",
)
EXCLUDED_DIRS: frozenset[str] = frozenset(
    unicodedata.normalize("NFC", d).casefold() for d in _EXCLUDED_DIR_NAMES
)

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_APOSTROPHE_INNER_RE = re.compile(r"(?<=\w)['’](?=\w)")
_CONCEPT_FILENAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]]')

_TURKISH_FOLD_TABLE = str.maketrans(
    {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
    }
)

_TR_STOPWORDS: frozenset[str] = frozenset(
    {
        "ve",
        "veya",
        "ile",
        "bir",
        "bu",
        "şu",
        "o",
        "da",
        "de",
        "ta",
        "te",
        "ki",
        "mi",
        "mı",
        "mu",
        "mü",
        "ne",
        "için",
        "icin",
        "gibi",
        "daha",
        "çok",
        "cok",
        "en",
        "ama",
        "fakat",
        "hem",
        "ya",
        "ise",
        "ben",
        "sen",
        "biz",
        "siz",
        "var",
        "yok",
        "olan",
        "olarak",
        "üzerine",
        "uzerine",
        "hakkında",
        "hakkinda",
        "nasıl",
        "nasil",
        "nedir",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
    }
)

_SHORT_TR_SUFFIXES: frozenset[str] = frozenset(
    {
        "",
        "ı",
        "i",
        "u",
        "ü",
        "a",
        "e",
        "da",
        "de",
        "ta",
        "te",
        "dan",
        "den",
        "tan",
        "ten",
        "ın",
        "in",
        "un",
        "ün",
        "nın",
        "nin",
        "nun",
        "nün",
        "na",
        "ne",
        "ya",
        "ye",
        "yı",
        "yi",
        "yu",
        "yü",
        "la",
        "le",
        "yla",
        "yle",
        "lar",
        "ler",
        "ları",
        "leri",
        "ım",
        "im",
        "um",
        "üm",
        "sı",
        "si",
        "su",
        "sü",
        "ki",
        "lı",
        "li",
        "lu",
        "lü",
    }
)

_SOFTEN_MAP = {"k": "ğ", "p": "b", "ç": "c", "t": "d"}

FTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    rel_path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'note'
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    rel_path UNINDEXED,
    title,
    body,
    search_folded,
    folder UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def fold_turkish(text: str) -> str:
    """Türkçe karakterleri (`çğıöşü`, `İ/I`) arama için ASCII karşılıklarına katlar."""
    return turkish_lower(text).translate(_TURKISH_FOLD_TABLE)


def _tokenize_tr(text: str) -> list[str]:
    lowered = turkish_lower(text)
    cleaned = _APOSTROPHE_INNER_RE.sub("", lowered)
    return [tok for tok in _TOKEN_RE.findall(cleaned) if tok]


def _tokenize_ascii_lower(text: str) -> list[str]:
    cleaned = _APOSTROPHE_INNER_RE.sub("", _nfc(text))
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(cleaned):
        if raw.isascii():
            tokens.append(raw.lower())
    return tokens


def _word_stem_match(term: str, candidate: str) -> bool:
    """Türkçe küçük harf + 5 karakterlik kök öneki (ve yumuşama/kısa ek) eşlemesi."""
    if not term or not candidate:
        return False
    if candidate == term:
        return True
    if len(term) >= 5 and len(candidate) >= 5:
        if candidate[:5] == term[:5]:
            return True
        if term[-1] in _SOFTEN_MAP:
            softened = term[:-1] + _SOFTEN_MAP[term[-1]]
            if candidate.startswith(softened):
                return True
        return False
    if len(term) >= 3 and candidate.startswith(term):
        suffix = candidate[len(term) :]
        if suffix in _SHORT_TR_SUFFIXES:
            return True
    return False


def _term_matches_text(
    term: str,
    text_tr_lower: str,
    text_tr_words: list[str],
    text_ascii_words: list[str],
) -> bool:
    term_l = turkish_lower(term).strip()
    if not term_l:
        return False

    term_words = _tokenize_tr(term_l)
    if not term_words:
        return False

    if len(term_words) > 1:
        if term_l in text_tr_lower:
            return True
        return all(
            any(_word_stem_match(tw, cw) for cw in text_tr_words) for tw in term_words
        )

    single = term_words[0]
    if any(_word_stem_match(single, cw) for cw in text_tr_words):
        return True
    if single.isascii() and any(_word_stem_match(single, aw) for aw in text_ascii_words):
        return True
    return False


def _concept_filename(title: str) -> str:
    return _nfc(_CONCEPT_FILENAME_RE.sub("-", title).strip() or "kavram")


def _extract_anchor_and_excerpt(
    body: str,
    query_terms: list[str],
    max_chars: int = 240,
) -> tuple[str, str]:
    clean_body = body.strip()
    if not clean_body:
        return "", ""

    folded_body = fold_turkish(clean_body)
    best_pos = -1
    for term in query_terms:
        ft = fold_turkish(term)
        if not ft:
            continue
        needle = ft[:5] if len(ft) >= 5 else ft
        pos = folded_body.find(needle)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    anchor = ""
    if best_pos != -1:
        line_end = clean_body.find("\n", best_pos)
        prefix = clean_body[: line_end if line_end != -1 else len(clean_body)]
        headings = re.findall(r"^#{1,6}\s+(.+)$", prefix, flags=re.MULTILINE)
        if headings:
            anchor = headings[-1].strip()
        start = max(0, best_pos - 60)
        end = min(len(clean_body), start + max_chars)
        snippet = clean_body[start:end].strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(clean_body):
            snippet = snippet + "…"
    else:
        snippet = clean_body[:max_chars].strip()
        if len(clean_body) > max_chars:
            snippet = snippet + "…"

    snippet = re.sub(r"\s+", " ", snippet).strip()
    return anchor, snippet


def _build_fts_match_expr(query: str) -> tuple[str, list[str]]:
    raw_tokens = _tokenize_tr(query)
    filtered = [t for t in raw_tokens if len(t) >= 2 and t not in _TR_STOPWORDS]
    tokens = filtered or [t for t in raw_tokens if len(t) >= 2] or raw_tokens
    if not tokens:
        return "", []

    clauses: list[str] = []
    seen: set[str] = set()
    used_tokens: list[str] = []

    for tok in tokens[:12]:
        folded = fold_turkish(tok)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        used_tokens.append(tok)

        safe_tok = tok.replace('"', "")
        safe_folded = folded.replace('"', "")
        variants: list[str] = []
        if safe_tok:
            variants.append(f'"{safe_tok}"')
        if safe_folded and safe_folded != safe_tok:
            variants.append(f'"{safe_folded}"')
        if len(safe_folded) >= 5:
            variants.append(f'"{safe_folded[:5]}"*')
        elif len(safe_folded) >= 3:
            variants.append(f'"{safe_folded}"*')

        if variants:
            clauses.append("(" + " OR ".join(dict.fromkeys(variants)) + ")")

    return " OR ".join(clauses), used_tokens


class VaultIndex:
    """Obsidian / Hafıza OS kasası için kasa-dışı artımlı FTS5 arama ve bağlantı motoru."""

    def __init__(self, vault_path: Path, fts_db_path: Path) -> None:
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.fts_db_path = Path(fts_db_path).expanduser().resolve()
        if self.fts_db_path.is_relative_to(self.vault_path):
            raise ValueError(
                f"FTS5 veritabanı kasa içinde tutulamaz: {self.fts_db_path}"
            )
        self.fts_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.fts_db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(FTS_SCHEMA_SQL)

    def _classify_kind(self, folder: str) -> Literal["catalog", "bilgi", "note", "concept"]:
        folder_cf = _nfc(folder).casefold()
        if folder_cf == "bilgi":
            return "bilgi"
        if folder_cf == "beyin":
            return "concept"
        return "note"

    def _iter_indexable_files(self) -> list[tuple[str, Path, int, int, str]]:
        if not self.vault_path.is_dir():
            return []

        discovered: list[tuple[str, Path, int, int, str]] = []
        for dirpath, dirnames, filenames in os.walk(self.vault_path, followlinks=False):
            current = Path(dirpath)
            pruned: list[str] = []
            for dname in dirnames:
                d_norm = _nfc(dname).casefold()
                if dname.startswith(".") or d_norm in EXCLUDED_DIRS:
                    continue
                if (current / dname).is_symlink():
                    continue
                pruned.append(dname)
            dirnames[:] = sorted(pruned)

            for fname in sorted(filenames):
                if fname.startswith(".") or not fname.lower().endswith(".md"):
                    continue
                fpath = current / fname
                if fpath.is_symlink():
                    continue
                try:
                    resolved = fpath.resolve()
                except OSError:
                    continue
                if not resolved.is_relative_to(self.vault_path):
                    continue
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                rel = _normalize_rel_posix(str(fpath.relative_to(self.vault_path)))
                parts = PurePosixPath(rel).parts
                folder = _nfc(parts[0]) if len(parts) > 1 else ""
                discovered.append((rel, fpath, st.st_mtime_ns, st.st_size, folder))
        return discovered

    def sync(self) -> dict[str, int]:
        """Kasa dosyalarını `(rel_path, mtime_ns, size_bytes)` bazında artımlı senkronize eder."""
        stats = {
            "added": 0,
            "updated": 0,
            "deleted": 0,
            "unchanged": 0,
            "skipped_secret": 0,
        }

        discovered = self._iter_indexable_files()
        seen_paths: set[str] = set()

        with self._connect() as conn:
            existing: dict[str, tuple[int, int]] = {
                row["rel_path"]: (row["mtime_ns"], row["size_bytes"])
                for row in conn.execute("SELECT rel_path, mtime_ns, size_bytes FROM files").fetchall()
            }

            for rel_path, fpath, mtime_ns, size_bytes, folder in discovered:
                prev = existing.get(rel_path)
                if prev is not None and prev == (mtime_ns, size_bytes):
                    seen_paths.add(rel_path)
                    stats["unchanged"] += 1
                    continue

                try:
                    raw_text = _nfc(fpath.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    continue

                if contains_secret(raw_text):
                    stats["skipped_secret"] += 1
                    if prev is not None:
                        conn.execute("DELETE FROM files WHERE rel_path = ?", (rel_path,))
                        conn.execute("DELETE FROM docs_fts WHERE rel_path = ?", (rel_path,))
                        stats["deleted"] += 1
                    continue

                fm = _parse_flat_frontmatter(raw_text)
                body = _FRONTMATTER_RE.sub("", raw_text).strip()
                title = _extract_h1_title(raw_text, fpath.stem)
                kind = self._classify_kind(folder)

                # Ön-madde etiket/konu/kavram değerlerini katlanmış aramaya ekle
                meta_extra = " ".join(
                    fm.get(k, "") for k in ("konu", "kavramlar", "etiketler", "kapsam") if fm.get(k)
                )
                search_folded = fold_turkish(f"{title} {meta_extra} {body}")

                conn.execute("DELETE FROM docs_fts WHERE rel_path = ?", (rel_path,))
                conn.execute(
                    """
                    INSERT INTO docs_fts (rel_path, title, body, search_folded, folder)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (rel_path, title, body, search_folded, folder),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO files (rel_path, mtime_ns, size_bytes, title, folder, kind)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rel_path, mtime_ns, size_bytes, title, folder, kind),
                )
                seen_paths.add(rel_path)
                if prev is None:
                    stats["added"] += 1
                else:
                    stats["updated"] += 1

            # Diskten silinen kayıtları kaldır
            for old_rel in set(existing) - seen_paths:
                # Sır yüzünden az önce silinenleri iki kez sayma
                cur_exists = conn.execute(
                    "SELECT 1 FROM files WHERE rel_path = ?", (old_rel,)
                ).fetchone()
                if cur_exists is not None:
                    conn.execute("DELETE FROM files WHERE rel_path = ?", (old_rel,))
                    conn.execute("DELETE FROM docs_fts WHERE rel_path = ?", (old_rel,))
                    stats["deleted"] += 1

        return stats

    def search(
        self,
        query: str,
        limit: int = 8,
        folders: list[str] | None = None,
    ) -> list[VaultContextHit]:
        """FTS5 BM25 sıralamalı arama yapar ve `V01..Vn` kayıtları döndürür."""
        if limit <= 0 or not query or not query.strip():
            return []

        match_expr, used_tokens = _build_fts_match_expr(query)
        if not match_expr:
            return []

        params: list[Any] = [match_expr]
        folder_clause = ""
        if folders:
            norm_folders = [_nfc(f.strip("/")) for f in folders if f.strip("/")]
            if norm_folders:
                placeholders = ",".join("?" for _ in norm_folders)
                folder_clause = f" AND docs_fts.folder IN ({placeholders})"
                params.extend(norm_folders)

        params.append(limit)
        sql = f"""
            SELECT
                docs_fts.rel_path AS rel_path,
                docs_fts.title AS title,
                docs_fts.body AS body,
                files.kind AS kind,
                bm25(docs_fts, 0.0, 4.0, 2.0, 1.0, 0.0) AS rank_score
            FROM docs_fts
            JOIN files ON files.rel_path = docs_fts.rel_path
            WHERE docs_fts MATCH ? {folder_clause}
            ORDER BY rank_score ASC
            LIMIT ?
        """

        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

        hits: list[VaultContextHit] = []
        for idx, row in enumerate(rows, start=1):
            anchor, excerpt = _extract_anchor_and_excerpt(row["body"], used_tokens)
            raw_rank = float(row["rank_score"]) if row["rank_score"] is not None else 0.0
            score = round(abs(raw_rank), 4)
            kind_val = row["kind"] if row["kind"] in ("catalog", "bilgi", "note", "concept") else "note"
            hits.append(
                VaultContextHit(
                    source_id=f"V{idx:02d}",
                    path=row["rel_path"],
                    title=row["title"] or Path(row["rel_path"]).stem,
                    anchor=anchor,
                    excerpt=excerpt,
                    score=score,
                    kind=kind_val,
                )
            )
        return hits

    def _load_json_if_exists(self, rel_path: str) -> dict[str, Any]:
        target = self.vault_path / rel_path
        if not target.is_file() or target.is_symlink():
            return {}
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def match_concepts_and_projects(
        self,
        text: str,
    ) -> tuple[str, list[str], list[str], list[str]]:
        """`komuta/kavramlar.json` ve `komuta/gorev-baglam.json` üzerinden eşleme yapar.

        Türkçe küçük harf (`İ->i, I->ı`) + 5 karakter kök öneki eşlemesi kullanarak
        `(scope, matched_concept_ids, matched_project_ids, expanded_aliases)` döndürür.
        """
        if not text or not text.strip():
            return "user", [], [], []

        text_tr_lower = turkish_lower(text)
        text_tr_words = _tokenize_tr(text)
        text_ascii_words = _tokenize_ascii_lower(text)

        matched_concept_ids: list[str] = []
        matched_project_ids: list[str] = []
        active_project_ids: list[str] = []
        expanded_aliases: list[str] = []
        seen_aliases: set[str] = set()

        def _add_alias(term: str) -> None:
            clean = _nfc(term.strip())
            key = turkish_lower(clean)
            if clean and key not in seen_aliases:
                seen_aliases.add(key)
                expanded_aliases.append(clean)

        # 1. Kavramlar (komuta/kavramlar.json)
        kavram_data = self._load_json_if_exists("komuta/kavramlar.json")
        kavramlar = kavram_data.get("kavramlar")
        if isinstance(kavramlar, list):
            for concept in kavramlar:
                if not isinstance(concept, dict):
                    continue
                cid = concept.get("id")
                if not isinstance(cid, str) or not cid.strip():
                    continue
                selectors = concept.get("selectors") or []
                aliases = concept.get("aliases") or []
                terms = [t for t in [*selectors, *aliases] if isinstance(t, str)]
                if any(
                    _term_matches_text(t, text_tr_lower, text_tr_words, text_ascii_words)
                    for t in terms
                ):
                    if cid not in matched_concept_ids:
                        matched_concept_ids.append(cid)
                    for alias in aliases:
                        if isinstance(alias, str):
                            _add_alias(alias)

        # 2. Projeler (komuta/gorev-baglam.json)
        baglam_data = self._load_json_if_exists("komuta/gorev-baglam.json")
        projects = baglam_data.get("projects")
        if isinstance(projects, list):
            for project in projects:
                if not isinstance(project, dict):
                    continue
                pid = project.get("id")
                if not isinstance(pid, str) or not pid.strip():
                    continue
                aliases = project.get("aliases") or []
                candidate_terms = [pid, pid.replace("-", " ")]
                candidate_terms.extend(a for a in aliases if isinstance(a, str))
                if any(
                    _term_matches_text(t, text_tr_lower, text_tr_words, text_ascii_words)
                    for t in candidate_terms
                ):
                    if pid not in matched_project_ids:
                        matched_project_ids.append(pid)
                    status = str(project.get("status", "active")).casefold()
                    if status != "archived" and pid not in active_project_ids:
                        active_project_ids.append(pid)
                    for alias in aliases:
                        if isinstance(alias, str):
                            _add_alias(alias)

        if len(active_project_ids) == 1:
            scope = f"project:{active_project_ids[0]}"
        elif not active_project_ids and len(matched_project_ids) == 1:
            scope = f"project:{matched_project_ids[0]}"
        else:
            scope = "user"

        return scope, matched_concept_ids, matched_project_ids, expanded_aliases

    def discover_wikilinks(
        self,
        text: str,
        *,
        exclude_rel_path: str = "",
        limit: int = 5,
    ) -> list[tuple[str, str]]:
        """Doğrulanmış mevcut `(rel_path_without_md, display_title)` wikilink çiftleri bulur.

        Öncelik: eşleşen `beyin/kavramlar/<Başlık>.md`, `projeler/<id>/DURUM.md` ve
        FTS5'te en yüksek puanlı notlar. ASLA `Ana Sayfa`, `*Haritası.md` veya
        var olmayan (kırık) dosya hedeflerine bağlantı üretmez.
        """
        if limit <= 0 or not text or not text.strip():
            return []

        capped_limit = min(limit, 6)
        exclude_norm = _strip_md(exclude_rel_path) if exclude_rel_path else ""
        results: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _try_add(rel_with_md: str, display_title: str = "") -> bool:
            norm_rel = _normalize_rel_posix(rel_with_md)
            rel_no_md = _strip_md(norm_rel)
            if not rel_no_md or rel_no_md == exclude_norm or rel_no_md in seen:
                return False
            if _is_forbidden_link_target(rel_no_md):
                return False
            candidate_path = self.vault_path / f"{rel_no_md}.md"
            if candidate_path.is_symlink() or not candidate_path.is_file():
                return False
            try:
                if not candidate_path.resolve().is_relative_to(self.vault_path):
                    return False
            except OSError:
                return False

            title = _nfc(display_title.strip())
            if not title:
                try:
                    raw = _nfc(candidate_path.read_text(encoding="utf-8"))
                    title = _extract_h1_title(raw, candidate_path.stem)
                except OSError:
                    title = candidate_path.stem
            seen.add(rel_no_md)
            results.append((rel_no_md, title))
            return True

        _, matched_concepts, matched_projects, expanded_aliases = (
            self.match_concepts_and_projects(text)
        )

        # 1. Eşleşen kavramlar -> beyin/kavramlar/<Title>.md
        if matched_concepts:
            kavram_data = self._load_json_if_exists("komuta/kavramlar.json")
            title_by_id = {
                c["id"]: c.get("title", c["id"])
                for c in kavram_data.get("kavramlar", [])
                if isinstance(c, dict) and isinstance(c.get("id"), str)
            }
            for cid in matched_concepts:
                if len(results) >= capped_limit:
                    break
                ctitle = str(title_by_id.get(cid, cid))
                fname = _concept_filename(ctitle)
                _try_add(f"beyin/kavramlar/{fname}.md", ctitle)

        # 2. Eşleşen projeler -> projeler/<id>/DURUM.md
        for pid in matched_projects:
            if len(results) >= capped_limit:
                break
            _try_add(f"projeler/{pid}/DURUM.md", "")

        # 3. En yüksek puanlı FTS5 notları
        if len(results) < capped_limit:
            with self._connect() as conn:
                indexed_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            if indexed_count == 0:
                self.sync()

            enriched = text
            if expanded_aliases:
                enriched = f"{text} {' '.join(expanded_aliases[:4])}"
            hits = self.search(enriched, limit=capped_limit * 3)
            for hit in hits:
                if len(results) >= capped_limit:
                    break
                _try_add(hit.path, hit.title)

        return results

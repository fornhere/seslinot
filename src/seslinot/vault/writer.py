"""Obsidian / Hafıza OS uyumlu güvenli kasa yazıcısı.

Yalnızca aktif SesliNot kök klasörlerine (`sesli-notlar/` ve `araştırmalar/`)
düz Türkçe YAML ön-madde, sır maskeleme, hash güvenlik denetimi ve atomik
kilitli yazım ile dosya yazar.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from seslinot.config import SesliNotConfig
from seslinot.models import ResearchReportInput, VoiceNoteRecord

# --- Yasaklı Hafıza OS dizinleri (asla doğrudan yazılmaz) ---

_FORBIDDEN_WRITE_DIR_NAMES = (
    "bilgi",
    "beyin",
    "zihin",
    "gelen-kutusu",
    "günlük",
    ".obsidian",
    ".git",
    "araclar",
)
FORBIDDEN_WRITE_DIRS: frozenset[str] = frozenset(
    unicodedata.normalize("NFC", name).casefold() for name in _FORBIDDEN_WRITE_DIR_NAMES
)

# --- Sır tarama kuralları (Hafıza OS SECRET_RULES uyumlu + Bearer/PEM) ---

_SECRET_CONTEXT = (
    r"(?i:\b(?:anahtar(?:[ıi](?:m|n|m[ıi]z|n[ıi]z)?)?"
    r"|şifre(?:m|n|si|miz|niz)?|sifre|parola(?:m|n|sı|mız|nız)?"
    r"|kurtarma[ _-]+kod(?:u|um|un|umuz|unuz)?"
    r"|(?:api|access|secret)[ _-]?key|(?:access[ _-]?)?token"
    r"|(?:recovery|backup)[ _-]+code|password|passwd|passphrase|secret|key)\b)"
)
_SECRET_SEPARATOR = (
    r"(?:[ \t]*[\"'`*]{0,3}\s*[:=]+[*]{0,3}\s*[\"'`*]{0,3}"
    r"|[ \t]+(?:[-–—][ \t]+)?[\"'`*]{0,3})"
)
_SECRET_END = r"(?![\w/\\-]|\.\w)"
_MIXED_SECRET_GROUP = r"(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{3,8}"
_PLAIN_SECRET_GROUP = r"(?:[A-Za-z]{3,8}|[0-9]{3,8})"

SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z][A-Z0-9 ]* )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:[A-Z][A-Z0-9 ]* )?PRIVATE KEY-----"
            r"|-----BEGIN (?:[A-Z][A-Z0-9 ]* )?PRIVATE KEY-----"
        ),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE),
    ),
    (
        "provider_sk",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "provider_github",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    ),
    (
        "provider_slack",
        re.compile(r"\b(?:xox[abeprs]-|xapp-|xwfp-)[A-Za-z0-9-]{10,}"),
    ),
    (
        "provider_aws",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "provider_google",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![\w-])"),
    ),
    (
        "provider_gitlab",
        re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}"),
    ),
    (
        "provider_huggingface",
        re.compile(r"\bhf_[0-9A-Za-z]{30,}\b"),
    ),
    (
        "provider_vercel",
        re.compile(r"\b(?:vck|vcp)_[0-9A-Za-z_-]{20,}"),
    ),
    (
        "legacy_assignment",
        re.compile(
            r"\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?key|token|parola|şifre|sifre|"
            r"password|passwd|passphrase)\s*[:=]+\s*\S+",
            re.IGNORECASE,
        ),
    ),
    (
        "context_value",
        re.compile(
            _SECRET_CONTEXT
            + _SECRET_SEPARATOR
            + r"(?P<secret>(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{12,})"
            + _SECRET_END
        ),
    ),
    (
        "grouped_key",
        re.compile(
            r"(?<![\w/\\.-])"
            + rf"(?=(?:{_PLAIN_SECRET_GROUP}-)*{_MIXED_SECRET_GROUP}-"
            + rf"(?:{_PLAIN_SECRET_GROUP}-)*{_MIXED_SECRET_GROUP}(?:-|{_SECRET_END}))"
            + r"(?P<secret>[A-Za-z0-9]{3,8}(?:-[A-Za-z0-9]{3,8}){2,})"
            + _SECRET_END
        ),
    ),
)

_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]]')
_APOSTROPHE_INNER_RE = re.compile(r"(?<=\w)['’](?=\w)")
_SLUG_NON_WORD_RE = re.compile(r"[^\wçğıöşüâîû]+", re.UNICODE)
_YAML_SIMPLE_STR_RE = re.compile(r"^[A-Za-z0-9çğıöşüÇĞİÖŞÜâîûÂÎÛ._/-]+$")
_YAML_RESERVED_WORDS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "~"}
)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def turkish_lower(text: str) -> str:
    """Türkçe büyük/küçük harf dönüşümü (İ->i, I->ı) ve NFC normalizasyonu."""
    normalized = _nfc(text)
    mapped = normalized.replace("İ", "i").replace("I", "ı").lower()
    return _nfc(mapped.replace("i\u0307", "i"))


def turkish_slug(text: str, max_words: int = 7) -> str:
    """NFC normalizasyonlu, okunabilir Türkçe dosya adı slug'ı üretir."""
    if max_words <= 0:
        raise ValueError("max_words pozitif olmalıdır")

    cleaned = turkish_lower(text)
    cleaned = _ILLEGAL_FILENAME_RE.sub(" ", cleaned)
    cleaned = _APOSTROPHE_INNER_RE.sub("", cleaned)
    cleaned = cleaned.replace("_", " ")
    cleaned = _SLUG_NON_WORD_RE.sub(" ", cleaned)

    words = [w for w in cleaned.split() if w]
    if not words:
        return "not"

    slug = "-".join(words[:max_words])
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if len(slug) > 80:
        trimmed = slug[:80].rstrip("-")
        if "-" in trimmed:
            trimmed = trimmed.rsplit("-", 1)[0]
        slug = trimmed or slug[:80].rstrip("-")
    return _nfc(slug) or "not"


def scan_and_mask_secrets(text: str) -> tuple[str, list[str]]:
    """Metindeki sır kalıplarını tarar ve kimliksiz/idempotent yer tutucu ile maskeler.

    Hafıza OS `SECRET_RULES` tarayıcısının maskelenmiş çıktıda yeniden tetiklenmemesi
    için eşleşen sır ifadeleri `[MASKELENDİ:<kural>]` biçiminde değiştirilir.
    """
    if not text:
        return text, []

    masked = text
    matched_rules: list[str] = []

    for rule_name, pattern in SECRET_RULES:
        if pattern.search(masked):
            if rule_name not in matched_rules:
                matched_rules.append(rule_name)
            placeholder = f"[MASKELENDİ:{rule_name}]"
            masked = pattern.sub(placeholder, masked)

    return masked, matched_rules


def contains_secret(text: str) -> bool:
    """Metin herhangi bir sır kalıbı içeriyorsa True döner."""
    _, rules = scan_and_mask_secrets(text)
    return bool(rules)


def safe_vault_path(vault: Path, rel_path: str) -> Path:
    """Kasa içi göreli yolu fail-closed doğrular ve mutlak Path döndürür.

    Reddedilenler:
    - Boş, mutlak veya sürücü/kök öneki taşıyan yollar
    - `..` veya `.` barındıran üst dizin kaçışları
    - Yol veya üst dizinlerindeki herhangi bir symlink
    - `vault.resolve()` dışına taşan yollar
    - Yasaklı Hafıza OS dizinleri (`bilgi`, `beyin`, `zihin`, `gelen-kutusu`,
      `günlük`, `.obsidian`, `.git`, `araclar`)
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Göreli kasa yolu boş olamaz")
    if "\x00" in rel_path:
        raise ValueError("Kasa yolunda NUL baytı bulunamaz")

    raw = _nfc(rel_path.strip())
    if raw.startswith(("/", "\\")) or Path(raw).is_absolute():
        raise ValueError(f"Mutlak kasa yolu reddedildi: {rel_path}")
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"Sürücü öneki içeren yol reddedildi: {rel_path}")

    posix_norm = raw.replace("\\", "/")
    parts = [p for p in posix_norm.split("/") if p != ""]
    if not parts:
        raise ValueError(f"Geçersiz kasa yolu: {rel_path}")
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"Üst dizin kaçışı reddedildi: {rel_path}")

    first_norm = _nfc(parts[0]).casefold()
    if first_norm in FORBIDDEN_WRITE_DIRS:
        raise ValueError(f"Yasaklı Hafıza OS dizinine yazım reddedildi: {parts[0]}")
    for part in parts:
        if _nfc(part).casefold() in {".git", ".obsidian"}:
            raise ValueError(f"Sistem dizinine yazım reddedildi: {part}")

    vault_root = vault.expanduser().resolve()
    cur = vault_root
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"Symlink yolu reddedildi: {rel_path}")

    resolved = cur.resolve()
    if resolved == vault_root or not resolved.is_relative_to(vault_root):
        raise ValueError(f"Kasa kökü dışına çıkan yol reddedildi: {rel_path}")

    return resolved


def atomic_write_text(path: Path, content: str, lock_path: Path) -> None:
    """Harici `fcntl.flock` kilidi altında dot-prefixed temp + fsync + os.replace yazar.

    Kilit dosyası (`lock_path`) ASLA silinmez (unlink edilmez).
    """
    lock_file = Path(lock_path).expanduser()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    target = Path(path)

    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        target.parent.mkdir(parents=True, exist_ok=True)

        tmp_name = f".seslinot-{os.getpid()}-{secrets.token_hex(6)}.tmp"
        tmp_path = target.parent / tmp_name
        normalized_content = _nfc(content)

        try:
            with open(tmp_path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(normalized_content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
            try:
                dir_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _normalize_rel_posix(rel_path: str) -> str:
    cleaned = _nfc(rel_path.strip()).replace("\\", "/").lstrip("/")
    return str(PurePosixPath(cleaned))


def _strip_md(rel_path: str) -> str:
    norm = _normalize_rel_posix(rel_path)
    if norm.lower().endswith(".md"):
        return norm[:-3]
    return norm


def _normalize_wikilink_target(raw: str) -> str:
    s = raw.strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2].strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return _strip_md(s)


def _is_forbidden_link_target(target_without_md: str) -> bool:
    if not target_without_md or ".." in PurePosixPath(target_without_md).parts:
        return True
    name_cf = _nfc(PurePosixPath(target_without_md).name).casefold()
    if name_cf in {"ana sayfa", "readme", "oku", "index"}:
        return True
    if name_cf.endswith("haritası") or name_cf.endswith("haritasi"):
        return True
    return False


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if value is None:
        return '""'
    if isinstance(value, str):
        text = _nfc(value).replace("\r\n", " ").replace("\n", " ").strip()
        if not text:
            return '""'
        lower = text.casefold()
        if (
            lower in _YAML_RESERVED_WORDS
            or ":" in text
            or text.startswith(("-", "?", "[", "{", '"', "'", "#", "&", "*", "!", "|", ">", "%", "@", "`"))
            or not _YAML_SIMPLE_STR_RE.fullmatch(text)
            or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text)
        ):
            return json.dumps(text, ensure_ascii=False)
        return text
    raise ValueError(f"Düz YAML yalnızca skaler değerleri kabul eder, alınan: {type(value).__name__}")


def _render_flat_yaml(fields: list[tuple[str, Any]]) -> str:
    """Obsidian Bases (.base) uyumlu düz Türkçe YAML ön-madde bloğu üretir."""
    lines: list[str] = ["---"]
    for key, value in fields:
        norm_key = _nfc(key)
        if isinstance(value, dict):
            raise ValueError(f"İç içe YAML nesnesi yasaktır: {norm_key}")
        if isinstance(value, list):
            rendered_items: list[str] = []
            for item in value:
                if isinstance(item, (dict, list)):
                    raise ValueError(f"İç içe YAML listesi/nesnesi yasaktır: {norm_key}")
                rendered_items.append(_yaml_scalar(item))
            lines.append(f"{norm_key}: [{', '.join(rendered_items)}]")
        else:
            lines.append(f"{norm_key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _parse_flat_frontmatter(content: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = _nfc(k.strip())
        val = v.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            try:
                val = json.loads(val) if val[0] == '"' else val[1:-1]
            except json.JSONDecodeError:
                val = val[1:-1]
        result[key] = _nfc(str(val))
    return result


def _update_frontmatter(content: str, updates: dict[str, Any]) -> str:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        yaml_block = _render_flat_yaml(list(updates.items()))
        return yaml_block + content

    fm_lines = match.group(1).splitlines()
    body = content[match.end() :]
    remaining = {_nfc(k): v for k, v in updates.items()}
    new_lines: list[str] = []

    for line in fm_lines:
        if ":" in line:
            raw_key = _nfc(line.split(":", 1)[0].strip())
            if raw_key in remaining:
                val = remaining.pop(raw_key)
                if isinstance(val, list):
                    rendered = ", ".join(_yaml_scalar(x) for x in val)
                    new_lines.append(f"{raw_key}: [{rendered}]")
                else:
                    new_lines.append(f"{raw_key}: {_yaml_scalar(val)}")
                continue
        new_lines.append(line)

    for key, val in remaining.items():
        if isinstance(val, list):
            rendered = ", ".join(_yaml_scalar(x) for x in val)
            new_lines.append(f"{key}: [{rendered}]")
        else:
            new_lines.append(f"{key}: {_yaml_scalar(val)}")

    return "---\n" + "\n".join(new_lines) + "\n---\n" + body


def _extract_h1_title(content: str, fallback: str) -> str:
    body = _FRONTMATTER_RE.sub("", content)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return _nfc(stripped[2:].strip()) or fallback
    return fallback


def _parse_iso_dt(ts: str) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return datetime.now(timezone.utc)


def is_referenced_in_hafiza_memory(vault: Path, rel_path: str) -> bool:
    """Dosya `zihin/hafıza-kataloğu.jsonl` veya `bilgi/**/*.md` içinde referanslı mı?

    Hash bozulmasını önlemek için fail-closed çalışır: katalog veya bilgi notları
    okunamazsa ya da ilgili yol geçiyorsa True döndürür.
    """
    vault_root = vault.expanduser().resolve()
    target_rel = _normalize_rel_posix(rel_path)
    target_no_md = _strip_md(target_rel)
    candidates = {target_rel, target_no_md}

    # 1. zihin/hafıza-kataloğu.jsonl kontrolü
    catalog_path = vault_root / "zihin" / "hafıza-kataloğu.jsonl"
    if catalog_path.exists():
        try:
            raw_text = _nfc(catalog_path.read_text(encoding="utf-8"))
        except OSError:
            return True  # fail-closed
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                if target_rel in stripped or target_no_md in stripped:
                    return True
                continue
            if not isinstance(row, dict):
                continue
            src = row.get("source_path")
            if isinstance(src, str) and _normalize_rel_posix(src) in candidates:
                return True
            sources = row.get("sources")
            if isinstance(sources, list):
                for item in sources:
                    if isinstance(item, dict):
                        p = item.get("path")
                        if isinstance(p, str) and _normalize_rel_posix(p) in candidates:
                            return True
                    elif isinstance(item, str) and _normalize_rel_posix(item) in candidates:
                        return True

    # 2. bilgi/**/*.md kontrolü
    bilgi_dir = vault_root / "bilgi"
    if bilgi_dir.is_dir():
        try:
            md_files = sorted(bilgi_dir.rglob("*.md"))
        except OSError:
            return True  # fail-closed
        for md_file in md_files:
            if md_file.is_symlink():
                continue
            try:
                text = _nfc(md_file.read_text(encoding="utf-8"))
            except OSError:
                return True  # fail-closed
            if (
                f'"{target_rel}"' in text
                or f"[[{target_rel}]]" in text
                or f"[[{target_rel}|" in text
                or f"[[{target_no_md}]]" in text
                or f"[[{target_no_md}|" in text
                or target_rel in text
            ):
                return True

    return False


# --- Varsayılan banka iskeleti içerikleri ---

_VOICE_README_CONTENT = """---
tür: belge
---
# Sesli Notlar

Bu klasör **SesliNot** tarafından yakalanan sesli notların kasasıdır.

- Notlar `sesli-notlar/YYYY-AA/YYYY-AA-GG-SSDD-<slug>.md` biçiminde düz Türkçe YAML ile saklanır.
- Otomatik dizin haritası: [[sesli-notlar/Sesli Not Haritası|Sesli Not Haritası]]
- Obsidian Bases görünümü: [[sesli-notlar/Sesli Notlar.base|Sesli Notlar Tablosu]]
- Gizli anahtarlar, parolalar ve belirteçler yazım öncesinde otomatik maskelenir.
"""

_RESEARCH_README_CONTENT = """---
tür: belge
---
# Araştırmalar

Bu klasör **SesliNot** çok-ajanlı derin araştırma raporlarının kasasıdır.

- Raporlar `araştırmalar/YYYY-AA/YYYY-AA-GG-<slug>.md` biçiminde düz Türkçe YAML ile saklanır.
- Otomatik dizin haritası: [[araştırmalar/Araştırma Haritası|Araştırma Haritası]]
- Obsidian Bases görünümü: [[araştırmalar/Araştırmalar.base|Araştırmalar Tablosu]]
- Eski sürümler hafıza kataloğu hash güvenliği doğrulandıktan sonra `durum: yerini-aldı` olarak işaretlenir.
"""


def _voice_base_content(voice_dir: str) -> str:
    return f"""filters:
  and:
    - file.inFolder("{voice_dir}")
    - tür == "sesli-not"
properties:
  tarih:
    displayName: Tarih
  saat:
    displayName: Saat
  durum:
    displayName: Durum
  niyet:
    displayName: Niyet
  konu:
    displayName: Konu
  kapsam:
    displayName: Kapsam
  süre_sn:
    displayName: Süre (sn)
  ilgili_araştırma:
    displayName: İlgili Araştırma
views:
  - type: table
    name: Tüm Sesli Notlar
    order:
      - file.name
      - tarih
      - saat
      - durum
      - niyet
      - konu
      - kapsam
      - ilgili_araştırma
    sort:
      - property: tarih
        direction: DESC
      - property: saat
        direction: DESC
  - type: table
    name: Araştırılanlar
    filters:
      and:
        - durum == "araştırıldı"
    order:
      - file.name
      - tarih
      - konu
      - ilgili_araştırma
    sort:
      - property: tarih
        direction: DESC
"""


def _research_base_content(research_dir: str) -> str:
    return f"""filters:
  and:
    - file.inFolder("{research_dir}")
    - tür == "araştırma"
properties:
  tarih:
    displayName: Tarih
  son_doğrulama:
    displayName: Son Doğrulama
  durum:
    displayName: Durum
  konu:
    displayName: Konu
  kapsam:
    displayName: Kapsam
  güven:
    displayName: Güven
  denetim_durumu:
    displayName: Denetim
  kaynak_sayısı:
    displayName: Kaynak
  birincil_kaynak_sayısı:
    displayName: Birincil Kaynak
  tetik_notu:
    displayName: Tetik Notu
views:
  - type: table
    name: Güncel Araştırmalar
    filters:
      and:
        - durum == "güncel"
    order:
      - file.name
      - tarih
      - konu
      - güven
      - kaynak_sayısı
      - denetim_durumu
      - tetik_notu
    sort:
      - property: tarih
        direction: DESC
  - type: table
    name: Tüm Araştırmalar
    order:
      - file.name
      - tarih
      - durum
      - konu
      - kapsam
      - güven
      - kaynak_sayısı
    sort:
      - property: tarih
        direction: DESC
"""


class VaultWriter:
    """SesliNot -> Obsidian / Hafıza OS güvenli dosya ve harita yazıcısı."""

    def __init__(self, cfg: SesliNotConfig) -> None:
        self.cfg = cfg
        self.vault_path = Path(cfg.vault.path).expanduser()
        self.voice_dir = _nfc(cfg.vault.voice_dir.strip("/"))
        self.research_dir = _nfc(cfg.vault.research_dir.strip("/"))
        self.lock_path = Path(cfg.paths.vault_lock_path).expanduser()
        self._allowed_roots = frozenset(
            {_nfc(self.voice_dir).casefold(), _nfc(self.research_dir).casefold()}
        )

    def _allowed_write_path(self, rel_path: str) -> Path:
        target = safe_vault_path(self.vault_path, rel_path)
        norm_rel = _normalize_rel_posix(rel_path)
        top_dir = _nfc(PurePosixPath(norm_rel).parts[0]).casefold()
        if top_dir not in self._allowed_roots:
            raise ValueError(
                f"SesliNot yalnızca '{self.voice_dir}/' ve '{self.research_dir}/' "
                f"klasörlerine yazabilir; reddedilen: {rel_path}"
            )
        return target

    def ensure_bank_scaffolding(self) -> list[Path]:
        """`sesli-notlar/` ve `araştırmalar/` README + .base dosyalarını eksikse oluşturur."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        scaffolds: list[tuple[str, str]] = [
            (f"{self.voice_dir}/README.md", _VOICE_README_CONTENT),
            (f"{self.voice_dir}/Sesli Notlar.base", _voice_base_content(self.voice_dir)),
            (f"{self.research_dir}/README.md", _RESEARCH_README_CONTENT),
            (f"{self.research_dir}/Araştırmalar.base", _research_base_content(self.research_dir)),
        ]
        created: list[Path] = []
        for rel_path, content in scaffolds:
            target = self._allowed_write_path(rel_path)
            if not target.exists():
                atomic_write_text(target, content, self.lock_path)
                created.append(target)
        return created

    def _resolve_unique_rel_path(self, base_rel: str, record_id: str) -> tuple[Path, str]:
        """Aynı dakika/slug çakışmasında mevcut başka bir notu ezmeden benzersiz yol seçer."""
        abs_path = self._allowed_write_path(base_rel)
        if not abs_path.exists():
            return abs_path, base_rel

        try:
            existing_fm = _parse_flat_frontmatter(abs_path.read_text(encoding="utf-8"))
        except OSError:
            existing_fm = {}

        if existing_fm.get("kimlik") == record_id and not is_referenced_in_hafiza_memory(
            self.vault_path, base_rel
        ):
            return abs_path, base_rel

        stem = base_rel[:-3] if base_rel.endswith(".md") else base_rel
        id_suffix = turkish_slug(record_id, max_words=2).replace("-", "")[-6:] or "2"
        candidate_rel = f"{stem}-{id_suffix}.md"
        candidate_abs = self._allowed_write_path(candidate_rel)
        counter = 2
        while candidate_abs.exists():
            candidate_rel = f"{stem}-{id_suffix}-{counter}.md"
            candidate_abs = self._allowed_write_path(candidate_rel)
            counter += 1
        return candidate_abs, candidate_rel

    @staticmethod
    def _clean_wikilinks(
        wikilinks: list[tuple[str, str]] | None,
        *,
        exclude_rel_path: str = "",
    ) -> list[tuple[str, str]]:
        if not wikilinks:
            return []
        exclude_norm = _strip_md(exclude_rel_path) if exclude_rel_path else ""
        seen: set[str] = set()
        cleaned: list[tuple[str, str]] = []
        for raw_target, raw_title in wikilinks:
            target = _normalize_wikilink_target(raw_target)
            if not target or target == exclude_norm or target in seen:
                continue
            if _is_forbidden_link_target(target):
                continue
            seen.add(target)
            title = _nfc((raw_title or PurePosixPath(target).name).strip())
            cleaned.append((target, title))
        return cleaned

    @staticmethod
    def _format_wikilink_line(target: str, title: str) -> str:
        clean_title = title.replace("|", "-").replace("]", "").replace("[", "").strip()
        if clean_title and ("/" in target or clean_title != target):
            return f"- [[{target}|{clean_title}]]"
        return f"- [[{target}]]"

    def write_voice_note(
        self,
        note: VoiceNoteRecord,
        wikilinks: list[tuple[str, str]] | None = None,
    ) -> tuple[Path, str, list[str]]:
        """Sesli notu `sesli-notlar/YYYY-AA/YYYY-AA-GG-HHMM-<slug>.md` olarak yazar."""
        self.ensure_bank_scaffolding()

        dt = _parse_iso_dt(note.created_at)
        ym = dt.strftime("%Y-%m")
        ymd = dt.strftime("%Y-%m-%d")
        hm_colon = dt.strftime("%H:%M")
        hm_compact = dt.strftime("%H%M")

        masked_rules: list[str] = []

        def _mask(value: str) -> str:
            res, rules = scan_and_mask_secrets(value)
            for r in rules:
                if r not in masked_rules:
                    masked_rules.append(r)
            return res

        masked_title = _mask(note.title or "Sesli Not")
        masked_transcript = _mask(note.raw_transcript)
        masked_bullets = [_mask(b) for b in note.summary_bullets if b.strip()]
        masked_topic = _mask(note.topic or "genel")

        slug = turkish_slug(masked_title or masked_transcript, max_words=7)
        base_rel = _normalize_rel_posix(f"{self.voice_dir}/{ym}/{ymd}-{hm_compact}-{slug}.md")
        abs_path, rel_path = self._resolve_unique_rel_path(base_rel, note.note_id)

        related_research_fm = ""
        if note.related_research.strip():
            rr_target = _normalize_wikilink_target(note.related_research)
            if rr_target and not _is_forbidden_link_target(rr_target):
                related_research_fm = f"[[{rr_target}]]"

        tags = list(dict.fromkeys([_nfc(t) for t in note.tags if t.strip()])) or ["sesli-not"]
        concepts = list(dict.fromkeys([_nfc(c) for c in note.concepts if c.strip()]))

        frontmatter = _render_flat_yaml(
            [
                ("tür", "sesli-not"),
                ("kimlik", note.note_id),
                ("durum", note.status.value),
                ("niyet", note.intent.value),
                ("tarih", ymd),
                ("saat", hm_colon),
                ("süre_sn", int(note.duration_secs)),
                ("dil", note.language),
                ("stt_modeli", note.stt_model),
                ("kapsam", note.scope),
                ("konu", masked_topic),
                ("kavramlar", concepts),
                ("etiketler", tags),
                ("ilgili_araştırma", related_research_fm),
                ("hafıza_adayı_önerildi", bool(note.candidate_suggested)),
            ]
        )

        summary_lines = [f"- {b}" for b in masked_bullets]
        if not summary_lines:
            first_line = next(
                (ln.strip() for ln in masked_transcript.splitlines() if ln.strip()),
                masked_title,
            )
            summary_lines = [f"- {first_line[:220]}"]

        cleaned_links = self._clean_wikilinks(wikilinks, exclude_rel_path=rel_path)
        link_lines = [
            self._format_wikilink_line(t, _mask(title)) for t, title in cleaned_links
        ]

        sections = [
            frontmatter.rstrip("\n"),
            f"# {masked_title}",
            "",
            "## Özet",
            "",
            "\n".join(summary_lines),
            "",
            "## Ham Döküm",
            "",
            masked_transcript.strip(),
            "",
            "## İlgili Kasa Bağlantıları",
            "",
            "\n".join(link_lines) if link_lines else "- *(bağlantı bulunmadı)*",
            "",
        ]
        full_content = "\n".join(sections)

        # Son fail-closed sır maskeleme geçişi
        full_content = _mask(full_content)

        atomic_write_text(abs_path, full_content, self.lock_path)
        if self.cfg.vault.auto_update_maps:
            self.refresh_voice_map()
        return abs_path, rel_path, masked_rules

    def write_research_report(
        self,
        report: ResearchReportInput,
        wikilinks: list[tuple[str, str]] | None = None,
    ) -> tuple[Path, str, list[str]]:
        """Derin araştırma raporunu `araştırmalar/YYYY-AA/YYYY-AA-GG-<slug>.md` olarak yazar."""
        self.ensure_bank_scaffolding()

        dt = _parse_iso_dt(report.created_at)
        ym = dt.strftime("%Y-%m")
        ymd = dt.strftime("%Y-%m-%d")

        masked_rules: list[str] = []

        def _mask(value: str) -> str:
            res, rules = scan_and_mask_secrets(value)
            for r in rules:
                if r not in masked_rules:
                    masked_rules.append(r)
            return res

        masked_title = _mask(report.title)
        masked_topic = _mask(report.topic or "genel")
        masked_body = _mask(report.body_markdown.strip())

        slug = turkish_slug(report.topic_slug or masked_title, max_words=7)
        base_rel = _normalize_rel_posix(f"{self.research_dir}/{ym}/{ymd}-{slug}.md")
        abs_path, rel_path = self._resolve_unique_rel_path(base_rel, report.report_id)

        trigger_fm = ""
        trigger_target = ""
        if report.triggered_by_rel_path.strip():
            trigger_target = _normalize_wikilink_target(report.triggered_by_rel_path)
            if trigger_target and not _is_forbidden_link_target(trigger_target):
                trigger_fm = f"[[{trigger_target}]]"

        prev_fm = ""
        if report.previous_version_rel_path.strip():
            prev_target = _normalize_wikilink_target(report.previous_version_rel_path)
            if prev_target and not _is_forbidden_link_target(prev_target):
                prev_fm = f"[[{prev_target}]]"

        source_count = len(report.sources) if report.sources else report.verification.total_sources
        primary_source_count = sum(1 for src in report.sources if src.is_primary)
        verified_quotes = (
            report.verification.quote_exact_match + report.verification.quote_near_match
        )

        tags = list(dict.fromkeys([_nfc(t) for t in report.tags if t.strip()])) or ["araştırma"]
        concepts = list(dict.fromkeys([_nfc(c) for c in report.concepts if c.strip()]))

        frontmatter = _render_flat_yaml(
            [
                ("tür", "araştırma"),
                ("kimlik", report.report_id),
                ("durum", "güncel"),
                ("tarih", ymd),
                ("son_doğrulama", ymd),
                ("tazelik_ay", int(report.freshness_months)),
                ("kapsam", report.scope),
                ("konu", masked_topic),
                ("kavramlar", concepts),
                ("etiketler", tags),
                ("tetik_notu", trigger_fm),
                ("önceki_sürüm", prev_fm),
                ("yerine_geçen", ""),
                ("kaynak_sayısı", int(source_count)),
                ("birincil_kaynak_sayısı", int(primary_source_count)),
                ("doğrulanmış_alıntı", int(verified_quotes)),
                ("denetim_durumu", report.verification.audit_status),
                ("güven", report.confidence),
                ("ajan_sayısı", int(report.agent_count)),
                ("süre_sn", int(report.duration_secs)),
            ]
        )

        body_parts: list[str] = [frontmatter.rstrip("\n")]
        if not masked_body.startswith("# "):
            body_parts.append(f"# {masked_title}")
            body_parts.append("")
        body_parts.append(masked_body)

        if report.sources and "## Kaynaklar" not in masked_body:
            src_lines = ["", "## Kaynaklar", ""]
            for src in report.sources:
                src_title = _mask(src.title or src.canonical_url or src.vault_path or src.source_id)
                primary_badge = " · *birincil*" if src.is_primary else ""
                if src.source_type == "vault" and src.vault_path:
                    vtarget = _normalize_wikilink_target(src.vault_path)
                    if vtarget and not _is_forbidden_link_target(vtarget):
                        src_lines.append(
                            f"- **[{src.source_id}]** [[{vtarget}|{src_title}]] ({src.fetch_status}){primary_badge}"
                        )
                    else:
                        src_lines.append(
                            f"- **[{src.source_id}]** {src_title} ({src.fetch_status}){primary_badge}"
                        )
                else:
                    url = src.final_url or src.canonical_url
                    if url:
                        src_lines.append(
                            f"- **[{src.source_id}]** [{src_title}]({url}) ({src.fetch_status}){primary_badge}"
                        )
                    else:
                        src_lines.append(
                            f"- **[{src.source_id}]** {src_title} ({src.fetch_status}){primary_badge}"
                        )
            body_parts.append("\n".join(src_lines))

        combined_links: list[tuple[str, str]] = list(wikilinks or [])
        if trigger_target:
            combined_links.append((trigger_target, PurePosixPath(trigger_target).name))
        for raw_link in report.vault_context_links:
            norm_link = _normalize_wikilink_target(raw_link)
            if norm_link:
                combined_links.append((norm_link, PurePosixPath(norm_link).name))

        cleaned_links = self._clean_wikilinks(combined_links, exclude_rel_path=rel_path)
        if "## İlgili Kasa Bağlantıları" not in masked_body:
            link_lines = [
                self._format_wikilink_line(t, _mask(title)) for t, title in cleaned_links
            ]
            body_parts.extend(
                [
                    "",
                    "## İlgili Kasa Bağlantıları",
                    "",
                    "\n".join(link_lines) if link_lines else "- *(bağlantı bulunmadı)*",
                ]
            )

        full_content = "\n".join(body_parts).rstrip() + "\n"
        full_content = _mask(full_content)

        atomic_write_text(abs_path, full_content, self.lock_path)
        if self.cfg.vault.auto_update_maps:
            self.refresh_research_map()
        return abs_path, rel_path, masked_rules

    def link_research_to_voice_note(
        self,
        voice_rel_path: str,
        research_rel_path: str,
        research_title: str,
    ) -> bool:
        """Sesli nota araştırma bağlantısını hash-güvenli biçimde ekler.

        Sesli not `zihin/hafıza-kataloğu.jsonl` veya `bilgi/*.md` içinde
        referanslıysa dosyaya DOKUNMAZ ve False döner.
        """
        norm_voice = _normalize_rel_posix(voice_rel_path)
        voice_abs = self._allowed_write_path(norm_voice)
        if not voice_abs.is_file():
            return False

        if is_referenced_in_hafiza_memory(self.vault_path, norm_voice):
            return False

        research_target = _normalize_wikilink_target(research_rel_path)
        if not research_target or _is_forbidden_link_target(research_target):
            raise ValueError(f"Geçersiz araştırma hedefi: {research_rel_path}")

        masked_title, _ = scan_and_mask_secrets(research_title or PurePosixPath(research_target).name)
        link_line = self._format_wikilink_line(research_target, masked_title)

        content = _nfc(voice_abs.read_text(encoding="utf-8"))
        updated = _update_frontmatter(
            content,
            {
                "durum": "araştırıldı",
                "ilgili_araştırma": f"[[{research_target}]]",
            },
        )

        # Gövdede aynı bağlantı yoksa `## İlgili Kasa Bağlantıları` altına tek satır ekle
        body_only = _FRONTMATTER_RE.sub("", updated)
        if f"[[{research_target}]]" not in body_only and f"[[{research_target}|" not in body_only:
            heading = "## İlgili Kasa Bağlantıları"
            if heading in updated:
                updated = updated.replace("- *(bağlantı bulunmadı)*\n", "")
                updated = updated.replace("- *(bağlantı bulunmadı)*", "")
                updated = updated.rstrip("\n") + f"\n{link_line}\n"
            else:
                updated = updated.rstrip("\n") + f"\n\n{heading}\n\n{link_line}\n"

        updated, _ = scan_and_mask_secrets(updated)
        atomic_write_text(voice_abs, updated, self.lock_path)
        if self.cfg.vault.auto_update_maps:
            self.refresh_voice_map()
        return True

    def mark_previous_research_superseded(
        self,
        old_rel_path: str,
        new_rel_path: str,
    ) -> bool:
        """Eski araştırma sürümünü hash-güvenli biçimde `durum: yerini-aldı` yapar."""
        norm_old = _normalize_rel_posix(old_rel_path)
        old_abs = self._allowed_write_path(norm_old)
        if not old_abs.is_file():
            return False

        if is_referenced_in_hafiza_memory(self.vault_path, norm_old):
            return False

        new_target = _normalize_wikilink_target(new_rel_path)
        if not new_target or _is_forbidden_link_target(new_target):
            raise ValueError(f"Geçersiz yeni araştırma hedefi: {new_rel_path}")

        content = _nfc(old_abs.read_text(encoding="utf-8"))
        updated = _update_frontmatter(
            content,
            {
                "durum": "yerini-aldı",
                "yerine_geçen": f"[[{new_target}]]",
            },
        )
        updated, _ = scan_and_mask_secrets(updated)
        atomic_write_text(old_abs, updated, self.lock_path)
        if self.cfg.vault.auto_update_maps:
            self.refresh_research_map()
        return True

    def _collect_bank_entries(self, bank_dir: str, expected_type: str) -> list[dict[str, str]]:
        root = self.vault_path / bank_dir
        if not root.is_dir():
            return []

        entries: list[dict[str, str]] = []
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or path.name.startswith("."):
                continue
            name_cf = _nfc(path.stem).casefold()
            if name_cf in {"readme", "oku"} or name_cf.endswith(("haritası", "haritasi")):
                continue
            try:
                text = _nfc(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            fm = _parse_flat_frontmatter(text)
            if fm.get("tür") != expected_type:
                continue
            rel = _normalize_rel_posix(str(path.relative_to(self.vault_path)))
            entries.append(
                {
                    "rel_no_md": _strip_md(rel),
                    "title": _extract_h1_title(text, path.stem),
                    "tarih": fm.get("tarih", ""),
                    "saat": fm.get("saat", ""),
                    "durum": fm.get("durum", ""),
                    "konu": fm.get("konu", "genel") or "genel",
                    "güven": fm.get("güven", ""),
                    "kaynak_sayısı": fm.get("kaynak_sayısı", "0"),
                }
            )
        entries.sort(key=lambda e: (e["tarih"], e["saat"], e["rel_no_md"]), reverse=True)
        return entries

    def refresh_voice_map(self) -> Path:
        """`sesli-notlar/Sesli Not Haritası.md` tek-yönlü haritasını yeniler."""
        entries = self._collect_bank_entries(self.voice_dir, "sesli-not")
        map_rel = f"{self.voice_dir}/Sesli Not Haritası.md"
        map_abs = self._allowed_write_path(map_rel)

        status_counts: dict[str, int] = {}
        by_topic: dict[str, list[dict[str, str]]] = {}
        for item in entries:
            st = item["durum"] or "işlendi"
            status_counts[st] = status_counts.get(st, 0) + 1
            by_topic.setdefault(item["konu"], []).append(item)

        lines: list[str] = [
            "---",
            "tür: harita",
            "---",
            "# Sesli Not Haritası",
            "",
            f"SesliNot tarafından otomatik üretilir; elle düzenleme. "
            f"Sistem: [[{self.voice_dir}/README]] · Tablo: [[{self.voice_dir}/Sesli Notlar.base]]",
            "",
            "## Sayılar",
            "",
            "| Durum | Sayı |",
            "|---|---:|",
        ]
        if status_counts:
            for st in sorted(status_counts):
                lines.append(f"| {st} | {status_counts[st]} |")
        else:
            lines.append("| toplam | 0 |")

        for topic in sorted(by_topic):
            lines.extend(["", f"## {topic}", ""])
            for item in by_topic[topic]:
                meta = f"`{item['tarih']}`" if item["tarih"] else ""
                if item["durum"]:
                    meta = f"{meta} · `{item['durum']}`" if meta else f"`{item['durum']}`"
                suffix = f" · {meta}" if meta else ""
                lines.append(f"- [[{item['rel_no_md']}|{item['title']}]]{suffix}")

        content = "\n".join(lines).rstrip() + "\n"
        atomic_write_text(map_abs, content, self.lock_path)
        return map_abs

    def refresh_research_map(self) -> Path:
        """`araştırmalar/Araştırma Haritası.md` tek-yönlü haritasını yeniler."""
        entries = self._collect_bank_entries(self.research_dir, "araştırma")
        map_rel = f"{self.research_dir}/Araştırma Haritası.md"
        map_abs = self._allowed_write_path(map_rel)

        current_entries = [e for e in entries if e["durum"] != "yerini-aldı"]
        superseded_entries = [e for e in entries if e["durum"] == "yerini-aldı"]

        by_topic: dict[str, list[dict[str, str]]] = {}
        for item in current_entries:
            by_topic.setdefault(item["konu"], []).append(item)

        lines: list[str] = [
            "---",
            "tür: harita",
            "---",
            "# Araştırma Haritası",
            "",
            f"SesliNot tarafından otomatik üretilir; elle düzenleme. "
            f"Sistem: [[{self.research_dir}/README]] · Tablo: [[{self.research_dir}/Araştırmalar.base]]",
            "",
            "## Sayılar",
            "",
            "| Durum | Sayı |",
            "|---|---:|",
            f"| güncel | {len(current_entries)} |",
            f"| yerini-aldı | {len(superseded_entries)} |",
        ]

        for topic in sorted(by_topic):
            lines.extend(["", f"## {topic}", ""])
            for item in by_topic[topic]:
                parts: list[str] = []
                if item["tarih"]:
                    parts.append(f"`{item['tarih']}`")
                if item["güven"]:
                    parts.append(f"güven: `{item['güven']}`")
                if item["kaynak_sayısı"]:
                    parts.append(f"kaynak: `{item['kaynak_sayısı']}`")
                suffix = f" · {' · '.join(parts)}" if parts else ""
                lines.append(f"- [[{item['rel_no_md']}|{item['title']}]]{suffix}")

        if superseded_entries:
            lines.extend(["", "## Eski Sürümler", ""])
            for item in superseded_entries:
                tarih_meta = f" · `{item['tarih']}`" if item["tarih"] else ""
                lines.append(f"- [[{item['rel_no_md']}|{item['title']}]]{tarih_meta} · *yerini-aldı*")

        content = "\n".join(lines).rstrip() + "\n"
        atomic_write_text(map_abs, content, self.lock_path)
        return map_abs

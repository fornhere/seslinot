"""Stage 1: <1 ms deterministik Türkçe morfolojik ön-filtre."""

from __future__ import annotations

import re
import unicodedata

from seslinot.models import PrefilterResult

_TR_FOLD_TABLE = str.maketrans("çğıöşüâîû", "cgiosuaiu")
_WHITESPACE_RE = re.compile(r"\s+")


def tr_lower(text: str) -> str:
    """Türkçe duyarlı küçük harfe çevirme (İ->i, I->ı, NFC, birleştirme noktası temizliği)."""
    normalized = unicodedata.normalize("NFC", text)
    mapped = normalized.replace("İ", "i").replace("I", "ı").lower()
    return mapped.replace("\u0307", "")


def fold_tr(text: str) -> str:
    """Türkçe diakritikleri ASCII karşılıklarına indirger."""
    return tr_lower(text).translate(_TR_FOLD_TABLE)


# --- Açık olumsuzlama / sert engelleme desenleri (ÖNCE değerlendirilir) ---
_NEGATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # sadece/yalnızca/sırf not al / not et / not kalsın / kaydet / yaz
    re.compile(
        r"\b(?:sadece|yalnizca|sirf)\s+"
        r"(?:(?:bunu|sunu|onu|not\s+olarak)\s+)?"
        r"(?:not\s+(?:al[a-z]*|e[td][a-z]*|kalsin|tut[a-z]*|dus[a-z]*)"
        r"|kayde[td][a-z]*"
        r"|yaz(?:in|iniz|alim|sana|san|sin)?)\b"
    ),
    # araştır(ma) yapma / başlatma / istemiyorum / gerek yok / olmasın
    # NOT: "yapmanı istiyorum" / "yapmak" / "yapması" olumlu isim-fiildir; yalnızca olumsuz çekimler engellenir!
    re.compile(
        r"\baras[td]ir(?:ma(?:ya|yi|ni|si|sina|sini)?)?\s+"
        r"(?:(?:hic|falan|filan)\s+)?"
        r"(?:(?:yap|baslat|ac)ma(?:yin|yiniz|yalim|sin|sinlar|ya|mamali|mali|m[ae]n[a-z]*)?"
        r"|etme(?:yin|yiniz|yelim|sin|sinler|ye|memeli|meli|m[ae]n[a-z]*)?"
        r"|istemiyorum|istemem|istemedim"
        r"|(?:gerek|luzum|ihtiyac)\s+yok"
        r"|olmasin|degil)\b"
    ),
    # bunu/onu/şunu/sakın/hiç araştırma (olumsuz emir/istek)
    re.compile(
        r"\b(?:bunu|onu|sunu|sakin|hic)\s+"
        r"(?:(?:bunu|onu|sunu|simdi|su\s+an|sakin|hic)\s+)?"
        r"aras[td]irma(?:yin|yiniz|yalim|sin)?\b"
        r"(?!\s+(?:yap(?:in|iniz|alim|ar)?|baslat(?:in|iniz|alim)?|ac(?:in|alim)?|olustur|hazirla)\b)"
    ),
    # araştırmaya gerek yok
    re.compile(r"\baras[td]irmaya\s+(?:hic\s+)?(?:gerek|luzum|ihtiyac)\s+yok\b"),
    # araştırmadan kaydet / yaz / not al
    re.compile(
        r"\baras[td]irmadan\s+"
        r"(?:(?:bunu|onu|sunu|sadece|direkt|dogrudan)\s+)?"
        r"(?:kayde[td][a-z]*"
        r"|yaz(?:in|iniz|alim|sana|san|sin)?"
        r"|not\s+(?:al[a-z]*|e[td][a-z]*|tut[a-z]*|dus[a-z]*|kalsin))\b"
    ),
    # araştırmasın(lar)
    re.compile(r"\baras[td]irmasin(?:lar)?\b"),
    # not olarak kalsın / kaydet / dursun
    re.compile(r"\bnot\s+olarak\s+(?:kalsin|kayde[td][a-z]*|dursun|birak[a-z]*)\b"),
)

# --- Güçlü pozitif kök desenleri ---
_STRONG_STEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # araştır/araşdır fiil kökleri (isim formları: araştırma, araştırması, araştırmalar,
    # araştırmada, araştırmadan, araştırmayı, araştırmanın, araştırmaya, araştırmacı hariç)
    re.compile(
        r"\baras[td]ir(?!ma(?:ya|si[a-z]*|sin|lar[a-z]*|da|dan|yi|nin|ci[a-z]*)?\b)[a-z]*\b"
    ),
    # araştırma yap / başlat / aç / et / oluştur / hazırla
    re.compile(r"\baras[td]irma\s+(?:yap|baslat|ac|et|olustur|hazirla)[a-z]*\b"),
    # derinlemesine / detaylı(ca) / kapsamlı + araştır / incele / bak
    re.compile(
        r"\b(?:derinlemesine|detayli(?:ca)?|kapsamli)\s+"
        r"(?:aras[td]ir(?!ma(?:ya|si[a-z]*|sin|lar[a-z]*|da|dan|yi|nin|ci[a-z]*)?\b)"
        r"|incele(?!me(?:si[a-z]*|ler[a-z]*|de|den|yi|nin)?\b)"
        r"|bak)[a-z]*\b"
    ),
    # bunu / konuyu / şunu + [detaylı/kapsamlı/derinlemesine] + incele
    re.compile(
        r"\b(?:bunu|konuyu|sunu)\s+"
        r"(?:(?:detayli(?:ca)?|kapsamli|derinlemesine)\s+)?"
        r"incele[a-z]*\b"
    ),
)

# --- Yumuşak ipucu desenleri ---
_SOFT_CUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bkaynak(?:lari|lar)?\s+tara[a-z]*\b"),
    re.compile(r"\b(?:bir|bi)\s+karsilastir[a-z]*\b"),
    re.compile(r"\bkarsilastir(?:ip|ir\s+misin|abilir\s+misin|alim|sana)\b"),
    re.compile(r"\braporla(?:r\s+misin|yabilir\s+misin|yalim|sana)?\b"),
)


def _collect_matches(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for match in pat.finditer(text):
            token = match.group(0).strip()
            if token and token not in seen:
                seen.add(token)
                terms.append(token)
    return terms


def stage1_prefilter(raw_text: str) -> PrefilterResult:
    """Sesli not transkriptini LLM çağırmadan <1 ms'de hızlı ön-süzgeçten geçirir."""
    if not raw_text or not raw_text.strip():
        return PrefilterResult(matched=False, reason="no_match", matched_terms=[])

    folded = _WHITESPACE_RE.sub(" ", fold_tr(raw_text)).strip()

    neg_terms = _collect_matches(folded, _NEGATION_PATTERNS)
    if neg_terms:
        return PrefilterResult(
            matched=False,
            reason="explicit_negation",
            matched_terms=neg_terms,
        )

    strong_terms = _collect_matches(folded, _STRONG_STEM_PATTERNS)
    if strong_terms:
        return PrefilterResult(
            matched=True,
            reason="strong_stem",
            matched_terms=strong_terms,
        )

    soft_terms = _collect_matches(folded, _SOFT_CUE_PATTERNS)
    if soft_terms:
        return PrefilterResult(
            matched=True,
            reason="soft_cue",
            matched_terms=soft_terms,
        )

    return PrefilterResult(matched=False, reason="no_match", matched_terms=[])

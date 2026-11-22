"""Türkçe iki aşamalı niyet yönlendiricisi (Stage 1 + Stage 2) değerlendirme ve birim testleri."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from seslinot.config import SesliNotConfig
from seslinot.models import RouteDecision
from seslinot.router import (
    ROUTER_JSON_SCHEMA,
    IntentRouter,
    RouterTransientError,
    build_agy_command,
    build_claude_command,
    build_classifier_prompt,
    build_codex_command,
    fold_tr,
    parse_route_decision,
    stage1_prefilter,
    tr_lower,
)


# ---------------------------------------------------------------------------
# 1. Türkçe harf dönüşümü ve ASCII katlama (tr_lower / fold_tr)
# ---------------------------------------------------------------------------


def test_tr_lower_and_fold_tr_edge_cases() -> None:
    assert tr_lower("İSTANBUL") == "istanbul"
    assert tr_lower("IĞDIR") == "ığdır"
    assert tr_lower("ARAŞTIR") == "araştır"
    assert tr_lower("I\u0307NCELE") == "incele"
    assert tr_lower("i\u0307stanbul") == "istanbul"
    assert tr_lower("ARAS\u0327TIR") == "araştır"

    assert fold_tr("İSTANBUL ÇAĞRI ÖĞRENCİ ŞÜPHE ÂLÎ HÛ") == "istanbul cagri ogrenci suphe ali hu"
    assert fold_tr("ARAŞTIRMA YAPMA") == "arastirma yapma"
    assert fold_tr("I\u0307ŞIKLAR") == "isiklar"
    assert fold_tr("ARAS\u0327TIR") == "arastir"


# ---------------------------------------------------------------------------
# 2. 60+ Türkçe değerlendirme veri seti (Stage 1 ön-filtre doğrulaması)
# ---------------------------------------------------------------------------

POSITIVE_STRONG_CASES: list[str] = [
    # Açık emirler (19)
    "PipeWire düşük gecikme ayarlarını araştır.",
    "Hyprland NVIDIA flicker sorununu araştır.",
    "Claude Code hook sistemini derinlemesine araştır.",
    "Rust ve Zig bellek yönetimi farklarını araştırın.",
    "Bu konuyu araştırsana.",
    "Obsidian dataview eklentisi hakkında araştırma yap.",
    "SQLite WAL kilitleme davranışları üzerine araştırma başlat.",
    "Yeni yerel LLM modelleri için bir araştırma aç.",
    "Wayland ekran paylaşımı hakkında araştırma oluştur.",
    "MCP protokolü güvenlik modeli için araştırma hazırla.",
    "Bunu detaylı incele.",
    "Konuyu kapsamlı incele.",
    "Şunu derinlemesine incele.",
    "Bunu incele lütfen.",
    "Qwen3 benchmark sonuçlarına detaylıca bak.",
    "Derinlemesine incele şu dağıtık kuyruk mimarisini.",
    "Sadece Rust bellek güvenliğini araştır.",
    "Sadece yazılım mimarisini araştır.",
    "Kapsamlı bak şu yeni Vulkan sürücüsüne.",
    # Kibar sorular ve istek/yöneltme kipleri (8)
    "PipeWire quantum ayarlarını araştırır mısın?",
    "Whisper large v3 turbo gecikmesini araştırabilir misin?",
    "Bu yeni Linux çekirdek değişikliğini araştıralım.",
    "DeepSeek yeni sürümü hakkında araştırma yapalım.",
    "Konuyu inceleyebilir misin?",
    "Şu ajan mimarisini detaylıca araştıralım.",
    "Bu hatanın kök nedenini araştırır mısınız?",
    "Yarınki mimari karar için bunu araştırmamız lazım.",
    # Whisper yazım hataları, ASCII ve Türkçe İ/I büyük harf (9)
    "PİPEWİRE AYARLARINI ARAŞTIR",
    "IĞDIR İLE İLGİLİ DEĞİL, BUNU İNCELE",
    "hyprland eklentilerini arastir",
    "bu konuyu arasdir lutfen",
    "derinlemesine arasdirma yap",
    "BUNU I\u0307NCELE",
    "İSTANBUL TRAFİK VERİLERİNİ ARAŞTIRIR MISIN",
    "kapsamli arastiralim bu konuyu",
    "DETAYLICA İNCELE ŞU KONUYU",
    # Karışık notlar, çok cümleli notlar ve zamir çözümü (6)
    "Bugün toplantıda Ollama ile vLLM kıyaslandı, gecikme farkları ilginçti. Bunu araştırır mısın?",
    "Yarın sabah doktora gideceğim, not al. Bir de Omarchy Hyprland güncellemesini detaylıca araştır.",
    "YouTube için thumbnail fikri aklıma geldi, yüksek kontrast iyi çalışıyor. Şunu kapsamlı incele.",
    "Sabah kahve içerken düşündüm, yerel embeddings için BGE-M3 iyi diyorlar. Konuyu derinlemesine incele.",
    "Müşteri gecikmeden şikayet etti. Yeni önbellek stratejileri üzerine araştırma başlat, sonra toplantı ayarlarız.",
    "Yeni sunucuya NVMe disk taktık, performans ölçümü yapacağız. ZFS ve Btrfs farklarını araştıralım.",
]

POSITIVE_SOFT_CUE_CASES: list[str] = [
    "Bu konuda kaynak tara lütfen.",
    "Akademik literatürde kaynak tarayalım.",
    "Güncel kaynakları tara bakalım.",
    "Şu iki kütüphaneyi bir karşılaştırır mısın?",
    "PostgreSQL ve SQLite'ı bi karşılaştıralım.",
    "Opus ve Sonnet fiyatlarını karşılaştırıp özet çıkar.",
    "Bu iki yaklaşımı karşılaştıralım.",
    "Bulduklarını raporlar mısın?",
    "Performans test sonuçlarını raporlayalım.",
    "Şu kıyaslamayı raporla.",
    "Docker ve Podman güvenliğini karşılaştırır mısın?",
]

HARD_NEGATION_CASES: list[str] = [
    "PipeWire notu: sadece not al.",
    "Yalnızca not et, başka işlem olmasın.",
    "Sırf not kalsın, yarın bakarım.",
    "Sadece kaydet bunu.",
    "Yalnızca yaz bu fikri.",
    "Sadece bunu not al lütfen.",
    "Rust bellek modeli hakkında not: araştırma yapma.",
    "Bu konuda araştırma başlatma, kendim bakacağım.",
    "Şimdi araştırma istemiyorum, aklımda kalsın.",
    "Bunun için araştırma gerek yok.",
    "Araştırmaya gerek yok, elimde veri var.",
    "Araştırma olmasın, normal kayıt tut.",
    "Bunu araştırma, sadece hatırlatma olsun.",
    "Onu araştırma lütfen.",
    "Şunu araştırma, sonra konuşuruz.",
    "Sakın araştırma bunu.",
    "Sakın bunu araştırma.",
    "Hiç araştırma, geçici bir düşünce.",
    "Araştırmadan kaydet bunu.",
    "Araştırmadan yaz lütfen.",
    "Araştırmadan not al.",
    "Sistem bunu araştırmasın.",
    "Bu fikir not olarak kalsın.",
    "PipeWire araştır diyecektim ama vazgeçtim, araştırma yapma.",
]

NON_RESEARCH_EVERYDAY_CASES: list[str] = [
    # İsim formları (araştırma / araştırması / araştırmalar / araştırmada / araştırmadan / araştırmayı / araştırmanın / araştırmaya / araştırmacılar)
    "Bugün MIT araştırması okudum, çok ilginç sonuçlar vardı.",
    "Son araştırmalar gösteriyor ki düzenli uyku verimliliği artırıyor.",
    "Bu araştırmada kullanılan yöntem oldukça sade.",
    "Araştırmadan çıkan verilere göre büyüme yavaşladı.",
    "O araştırmayı kaydettim, hafta sonu okuyacağım.",
    "Araştırmanın özetini ekip toplantısında paylaştık.",
    "Yeni bir araştırma yayınlanmış, linkini sonra eklerim.",
    "Araştırmaya katılan kullanıcıların yarısı memnun kalmış.",
    "Araştırmacılar yeni bir katı hal pil kimyası geliştirdi.",
    "Son araştırmaları okurken güzel bir fikir geldi.",
    # Gündelik notlar, alışveriş, toplantı, fikir, geliştirme günlükleri
    "Marketten süt, yumurta, yoğurt ve tam buğday ekmek alınacak.",
    "Saat 15:00'te tasarım ekibiyle haftalık toplantı var.",
    "Yeni YouTube videosu için giriş fikri: terminal hızınızı ikiye katlayın.",
    "Claude Code'da yeni bir branch açtım, commit attım ve push ettim.",
    "Arabanın muayenesi gelecek ayın on beşinde, randevu al.",
    "Hafta sonu annemlere uğrayıp kitapları bırakacağım.",
    "Bugün spor salonunda sırt ve omuz çalıştım, çok iyi hissettim.",
    "Finans raporlar geldi, yarın sabah gözden geçireceğim.",
    "İki teklifi dün karşılaştırdım, ikincisi daha mantıklı görünüyor.",
    "Kapsamlı incelemesi geçen ay dergide yayınlandı.",
    "   ",
]

PAST_THIRD_QUOTED_CASES: list[tuple[str, str]] = [
    (
        "Dün gece PipeWire gecikme ayarlarını araştırdım, güzel bir çözüm buldum.",
        "geçmiş zaman anlatımı",
    ),
    (
        "Ahmet şu an yeni veritabanı seçeneklerini araştırıyor.",
        "3. şahıs anlatımı",
    ),
    (
        "Patron toplantıda 'bunu araştır' dedi ama görev bana değil Emre'ye verildi.",
        "aktarma/alıntı konuşma",
    ),
    (
        "Geçen hafta bu konuyu araştırmıştık zaten, notlarda duruyor.",
        "geçmiş zaman anlatımı",
    ),
    (
        "Ekip arkadaşım 'şunu kapsamlı incele' diye mesaj attı, yarın konuşacağız.",
        "aktarma/alıntı konuşma",
    ),
    (
        "Merve yeni yapay zeka araçlarını araştırıyormuş.",
        "3. şahıs anlatımı",
    ),
]


def test_eval_corpus_size_exceeds_60_turkish_cases() -> None:
    total = (
        len(POSITIVE_STRONG_CASES)
        + len(POSITIVE_SOFT_CUE_CASES)
        + len(HARD_NEGATION_CASES)
        + len(NON_RESEARCH_EVERYDAY_CASES)
        + len(PAST_THIRD_QUOTED_CASES)
    )
    assert total >= 90


@pytest.mark.parametrize("text", POSITIVE_STRONG_CASES)
def test_stage1_strong_stems_match(text: str) -> None:
    res = stage1_prefilter(text)
    assert res.matched is True, f"Beklenen strong_stem eşleşmedi: {text!r} -> {res}"
    assert res.reason == "strong_stem"
    assert len(res.matched_terms) >= 1


@pytest.mark.parametrize("text", POSITIVE_SOFT_CUE_CASES)
def test_stage1_soft_cues_match(text: str) -> None:
    res = stage1_prefilter(text)
    assert res.matched is True, f"Beklenen soft_cue eşleşmedi: {text!r} -> {res}"
    assert res.reason == "soft_cue"
    assert len(res.matched_terms) >= 1


@pytest.mark.parametrize("text", HARD_NEGATION_CASES)
def test_stage1_hard_negations_block(text: str) -> None:
    res = stage1_prefilter(text)
    assert res.matched is False, f"Olumsuzlama engellenemedi: {text!r} -> {res}"
    assert res.reason == "explicit_negation"
    assert len(res.matched_terms) >= 1


@pytest.mark.parametrize("text", NON_RESEARCH_EVERYDAY_CASES)
def test_stage1_everyday_and_noun_notes_do_not_match(text: str) -> None:
    res = stage1_prefilter(text)
    assert res.matched is False, f"Gündelik/isim notu yanlış eşleşti: {text!r} -> {res}"
    assert res.reason == "no_match"


def test_stage1_prefilter_latency_under_1ms() -> None:
    samples = POSITIVE_STRONG_CASES + HARD_NEGATION_CASES + NON_RESEARCH_EVERYDAY_CASES
    start = time.perf_counter()
    for text in samples:
        stage1_prefilter(text)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    avg_ms = elapsed_ms / len(samples)
    assert avg_ms < 1.0, f"Stage 1 ortalama süre <1 ms olmalı, ölçülen: {avg_ms:.3f} ms"


# ---------------------------------------------------------------------------
# 3. Stage 1 -> 0 LLM çağrısı garantisi & Stage 2 geçmiş zaman / 3. şahıs / alıntı
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SESLINOT_GEMINI_HTTP_MODEL", raising=False)


@pytest.mark.parametrize(
    "text,expected_reason",
    [
        ("Bugün MIT araştırması okudum.", "no_match"),
        ("Araştırmalar gösteriyor ki yürüyüş faydalı.", "no_match"),
        ("Marketten süt ve ekmek al.", "no_match"),
        ("Bunu araştırma, sadece not al.", "explicit_negation"),
        ("Araştırmaya gerek yok, elimde zaten not var.", "explicit_negation"),
        ("Araştırmadan kaydet bunu.", "explicit_negation"),
    ],
)
async def test_route_skips_stage2_with_zero_llm_calls(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected_reason: str,
) -> None:
    called = False

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("Stage 1 eşleşmediğinde LLM alt süreci çağrılmamalı!")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route(text)

    assert called is False
    assert decision.is_research is False
    assert decision.confidence == 0.0
    assert decision.intent_type == "note_only"
    assert decision.reason == expected_reason
    assert decision.backend_used == "prefilter"
    assert action == "note_only"


@pytest.mark.parametrize("text,reason_label", PAST_THIRD_QUOTED_CASES)
async def test_past_third_quoted_passes_stage1_and_stage2_returns_note_only(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    reason_label: str,
) -> None:
    pre = stage1_prefilter(text)
    assert pre.matched is True, f"Stage 1 bu vakayı Stage 2'ye devretmeli: {text!r}"

    mock_payload = {
        "is_research": False,
        "confidence": 0.91,
        "intent_type": "narrative_mention",
        "research_topic": "",
        "research_questions": [],
        "depth": "deep",
        "reason": reason_label,
    }

    async def fake_run_claude(self: IntentRouter, prompt: str, schema_str: str) -> RouteDecision:
        assert text.strip() in prompt
        return parse_route_decision(
            json.dumps(mock_payload, ensure_ascii=False), backend_used="claude"
        )

    monkeypatch.setattr(IntentRouter, "_run_claude", fake_run_claude)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route(text)
    assert decision.is_research is False
    assert decision.intent_type == "narrative_mention"
    assert decision.backend_used == "claude"
    assert action == "note_only"


async def test_mixed_note_pronoun_resolution_stage2(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = (
        "Bugün toplantıda Ollama ile vLLM kıyaslandı, özellikle VRAM kullanımı "
        "farkı çok ilginçti. Bunu araştırır mısın?"
    )
    mock_envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {
            "is_research": True,
            "confidence": 0.93,
            "intent_type": "question_implied",
            "research_topic": "Ollama ve vLLM VRAM kullanımı karşılaştırması",
            "research_questions": [
                "Ollama ve vLLM aynı modelde ne kadar VRAM tüketir?",
                "Eşzamanlı isteklerde bellek verimliliği nasıl değişir?",
            ],
            "depth": "deep",
            "reason": "Kibar soru kipi ve 'bunu' zamiri Ollama/vLLM konusuna çözülüyor.",
        },
        "result": "",
    }

    async def fake_subprocess(
        self: IntentRouter, cmd: list[str], *, stdin_text: str | None = None
    ) -> tuple[int, str, str]:
        return 0, json.dumps(mock_envelope, ensure_ascii=False), ""

    monkeypatch.setattr(IntentRouter, "_run_subprocess", fake_subprocess)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route(transcript)
    assert decision.is_research is True
    assert decision.confidence == 0.93
    assert decision.research_topic == "Ollama ve vLLM VRAM kullanımı karşılaştırması"
    assert len(decision.research_questions) == 2
    assert decision.backend_used == "claude"
    assert action == "auto_start"


async def test_force_stage2_bypasses_prefilter(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = "Acaba yeni Zig derleyici mimarisi nasıl çalışıyor?"
    assert stage1_prefilter(transcript).matched is False

    mock_payload = {
        "is_research": True,
        "confidence": 0.72,
        "intent_type": "question_implied",
        "research_topic": "Zig derleyici mimarisi",
        "research_questions": ["Zig derleyicisi hangi aşamalardan oluşur?"],
        "depth": "quick",
        "reason": "Örtük araştırma sorusu",
    }

    async def fake_subprocess(
        self: IntentRouter, cmd: list[str], *, stdin_text: str | None = None
    ) -> tuple[int, str, str]:
        return 0, json.dumps(mock_payload, ensure_ascii=False), ""

    monkeypatch.setattr(IntentRouter, "_run_subprocess", fake_subprocess)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route(transcript, force_stage2=True)
    assert decision.is_research is True
    assert decision.backend_used == "claude"
    assert action == "confirm"


# ---------------------------------------------------------------------------
# 4. Şema, istem ve CLI komut inşası güvenlik testleri
# ---------------------------------------------------------------------------


def test_router_json_schema_is_strict() -> None:
    assert ROUTER_JSON_SCHEMA["type"] == "object"
    assert ROUTER_JSON_SCHEMA["additionalProperties"] is False
    expected_fields = {
        "is_research",
        "confidence",
        "intent_type",
        "research_topic",
        "research_questions",
        "depth",
        "reason",
    }
    assert set(ROUTER_JSON_SCHEMA["required"]) == expected_fields
    assert set(ROUTER_JSON_SCHEMA["properties"].keys()) == expected_fields


def test_build_classifier_prompt_teaches_turkish_edge_cases() -> None:
    prompt = build_classifier_prompt("Bunu araştırır mısın?")
    assert "araştır" in prompt
    assert "araştırır mısın" in prompt
    assert "araştıralım" in prompt
    assert "araştırdım" in prompt
    assert "Ahmet" in prompt
    assert "dedi" in prompt
    assert "araştırma yapma" in prompt
    assert "bir araştırma okudum" in prompt
    assert "<transkript>" in prompt
    assert "Bunu araştırır mısın?" in prompt


def test_cli_command_construction_exact_safe_flags(tmp_path: Path) -> None:
    schema_str = json.dumps(ROUTER_JSON_SCHEMA, separators=(",", ":"))
    prompt = "PipeWire ayarlarını araştır."

    # 1) agy (--print=<prompt> bayraklardan sonra)
    agy_cmd = build_agy_command("gemini-3.8-flash-low", schema_str, prompt)
    assert agy_cmd == [
        "agy",
        "--model",
        "gemini-3.8-flash-low",
        "--effort",
        "low",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--json-schema",
        schema_str,
        f"--print={prompt}",
    ]


    # 2) claude — --bare ASLA olmamalı!
    claude_cmd = build_claude_command("haiku", schema_str, prompt)
    assert "--bare" not in claude_cmd
    assert claude_cmd[:2] == ["claude", "-p"]
    assert "--model" in claude_cmd and claude_cmd[claude_cmd.index("--model") + 1] == "haiku"
    assert "--effort" in claude_cmd and claude_cmd[claude_cmd.index("--effort") + 1] == "low"
    assert "--tools" in claude_cmd and claude_cmd[claude_cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in claude_cmd
    assert "--setting-sources" in claude_cmd and claude_cmd[claude_cmd.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in claude_cmd
    assert "--no-session-persistence" in claude_cmd
    assert "--output-format" in claude_cmd and claude_cmd[claude_cmd.index("--output-format") + 1] == "json"
    assert "--json-schema" in claude_cmd and claude_cmd[claude_cmd.index("--json-schema") + 1] == schema_str
    assert "--system-prompt" in claude_cmd
    assert claude_cmd[-1] == prompt

    # 3) codex — stdin ile "-", shell_tool kapalı, read-only, ephemeral
    schema_file = tmp_path / "schema.json"
    out_file = tmp_path / "out.json"
    codex_cmd = build_codex_command("gpt-6-luna", schema_file, out_file)
    assert codex_cmd == [
        "codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-m",
        "gpt-6-luna",
        "-c",
        'model_reasoning_effort="low"',
        "--disable",
        "shell_tool",
        "-s",
        "read-only",
        "--output-schema",
        str(schema_file),
        "-o",
        str(out_file),
        "-",
    ]


def test_prefilter_turkish_verbal_noun_vs_negation_regression() -> None:
    """'araştırma yapmanı istiyorum' olumlu mastardır; 'yapmamanı' olumsuz emirdir."""
    pos = stage1_prefilter("Evet şimdi senden bir araştırma yapmanı istiyorum.")
    assert pos.matched is True
    assert pos.reason == "strong_stem"

    pos2 = stage1_prefilter("Bu konuda kapsamlı bir araştırma başlatmanı istiyorum.")
    assert pos2.matched is True

    neg = stage1_prefilter("Bu konuda araştırma yapmamanı istiyorum.")
    assert neg.matched is False
    assert neg.reason == "explicit_negation"


# ---------------------------------------------------------------------------
# 5. JSON ve CLI zarf ayrıştırma (parse_route_decision)
# ---------------------------------------------------------------------------


def _sample_decision_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "is_research": True,
        "confidence": 0.88,
        "intent_type": "imperative",
        "research_topic": "PipeWire düşük gecikme",
        "research_questions": ["PipeWire quantum değeri nasıl ayarlanır?"],
        "depth": "deep",
        "reason": "Doğrudan araştırma emri",
    }
    base.update(overrides)
    return base


def test_parse_route_decision_direct_json() -> None:
    raw = json.dumps(_sample_decision_dict(), ensure_ascii=False)
    dec = parse_route_decision(raw, backend_used="codex")
    assert dec.is_research is True
    assert dec.confidence == 0.88
    assert dec.research_topic == "PipeWire düşük gecikme"
    assert dec.backend_used == "codex"


def test_parse_route_decision_structured_output_envelope() -> None:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": _sample_decision_dict(confidence=0.91),
        "result": "",
    }
    dec = parse_route_decision(json.dumps(envelope), backend_used="claude")
    assert dec.confidence == 0.91
    assert dec.backend_used == "claude"


def test_parse_route_decision_result_string_and_markdown_fence() -> None:
    inner = json.dumps(_sample_decision_dict(depth="quick"), ensure_ascii=False)
    envelope = {
        "type": "result",
        "is_error": False,
        "result": f"Aşağıda sonuç yer almaktadır:\n```json\n{inner}\n```",
    }
    dec = parse_route_decision(json.dumps(envelope), backend_used="agy")
    assert dec.depth == "quick"
    assert dec.backend_used == "agy"


def test_parse_route_decision_plain_markdown_fence() -> None:
    inner = json.dumps(_sample_decision_dict(confidence=0.64), ensure_ascii=False)
    raw = f"```json\n{inner}\n```"
    dec = parse_route_decision(raw, backend_used="agy")
    assert dec.confidence == 0.64


def test_parse_route_decision_ndjson_stream() -> None:
    line1 = json.dumps({"type": "system", "subtype": "init"})
    line2 = json.dumps({"type": "result", "structured_output": _sample_decision_dict(confidence=0.84)})
    dec = parse_route_decision(f"{line1}\n{line2}\n", backend_used="claude")
    assert dec.confidence == 0.84


def test_parse_route_decision_json_array_event_stream() -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "structured_output": _sample_decision_dict(confidence=0.83)},
    ]
    dec = parse_route_decision(json.dumps(events), backend_used="claude")
    assert dec.confidence == 0.83
    assert dec.backend_used == "claude"


def test_parse_route_decision_gemini_and_anthropic_envelopes() -> None:
    inner = json.dumps(_sample_decision_dict(confidence=0.87), ensure_ascii=False)
    gemini_env = {
        "candidates": [{"content": {"parts": [{"text": inner}]}}],
    }
    dec_g = parse_route_decision(json.dumps(gemini_env), backend_used="gemini_http")
    assert dec_g.confidence == 0.87
    assert dec_g.backend_used == "gemini_http"

    anthropic_env = {
        "content": [
            {
                "type": "tool_use",
                "name": "classify_intent",
                "input": _sample_decision_dict(confidence=0.92),
            }
        ]
    }
    dec_a = parse_route_decision(json.dumps(anthropic_env), backend_used="anthropic_http")
    assert dec_a.confidence == 0.92
    assert dec_a.backend_used == "anthropic_http"


def test_parse_route_decision_truncates_overlong_questions_and_rejects_errors() -> None:
    many_q = _sample_decision_dict(research_questions=[f"Soru {i}" for i in range(8)])
    dec = parse_route_decision(json.dumps(many_q), backend_used="agy")
    assert len(dec.research_questions) == 5

    err_env = {"type": "result", "is_error": True, "result": "429 rate limit exceeded"}
    with pytest.raises(ValueError, match="hata zarfı"):
        parse_route_decision(json.dumps(err_env), backend_used="claude")

    bad_conf = _sample_decision_dict(confidence=1.7)
    with pytest.raises(ValueError, match="doğrulama hatası"):
        parse_route_decision(json.dumps(bad_conf), backend_used="agy")


# ---------------------------------------------------------------------------
# 6. Fallback zinciri (agy -> claude -> codex), stdin/out_file, RouterTransientError
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
        on_communicate: Any = None,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._on_communicate = on_communicate
        self.killed = False
        self.received_input: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.received_input = input
        if self._hang:
            await asyncio.sleep(10)
        if self._on_communicate is not None:
            self._on_communicate(input)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


async def test_fallback_agy_to_claude_to_codex(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked: list[tuple[list[str], Any]] = []
    codex_stdin_captured: list[bytes | None] = []
    procs: list[_FakeProcess] = []

    codex_payload = json.dumps(
        _sample_decision_dict(confidence=0.89, research_topic="PipeWire düşük gecikme"),
        ensure_ascii=False,
    )

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        cmd_list = list(cmd)
        invoked.append((cmd_list, kwargs.get("stdin")))
        binary = cmd_list[0]

        if binary == "agy":
            # agy zaman aşımına uğrar
            proc = _FakeProcess(hang=True)
            procs.append(proc)
            return proc

        if binary == "claude":
            # --bare asla bulunmamalı
            assert "--bare" not in cmd_list
            # claude 429 döndürür
            proc = _FakeProcess(returncode=1, stderr=b"Error 429: Too Many Requests")
            procs.append(proc)
            return proc

        if binary == "codex":
            # codex stdin PIPE kullanmalı (DEVNULL değil!) ve -o dosyasına yazmalı
            assert kwargs.get("stdin") == asyncio.subprocess.PIPE
            out_idx = cmd_list.index("-o") + 1
            out_path = Path(cmd_list[out_idx])
            schema_idx = cmd_list.index("--output-schema") + 1
            schema_path = Path(cmd_list[schema_idx])
            assert schema_path.is_file()
            loaded_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            assert loaded_schema["additionalProperties"] is False

            def write_out(stdin_bytes: bytes | None) -> None:
                codex_stdin_captured.append(stdin_bytes)
                out_path.write_text(codex_payload, encoding="utf-8")

            proc = _FakeProcess(returncode=0, on_communicate=write_out)
            procs.append(proc)
            return proc

        raise AssertionError(f"Beklenmeyen ikili: {binary}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    cfg = SesliNotConfig()
    cfg.router.timeout_secs = 0.05
    router = IntentRouter(cfg)

    decision, action = await router.route("PipeWire düşük gecikme ayarlarını araştır.")

    assert [c[0][0] for c in invoked] == ["claude", "agy", "codex"]
    assert procs[1].killed is True  # agy zaman aşımında süreç sonlandırıldı
    assert len(codex_stdin_captured) == 1
    assert codex_stdin_captured[0] is not None
    assert "PipeWire düşük gecikme ayarlarını araştır." in codex_stdin_captured[0].decode("utf-8")

    assert decision.is_research is True
    assert decision.confidence == 0.89
    assert decision.backend_used == "codex"
    assert action == "auto_start"


async def test_fallback_claude_missing_binary_uses_agy(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    agy_envelope = {
        "type": "result",
        "is_error": False,
        "structured_output": _sample_decision_dict(confidence=0.68),
    }

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        seen.append(cmd[0])
        if cmd[0] == "claude":
            raise FileNotFoundError("claude not found")
        if cmd[0] == "agy":
            return _FakeProcess(
                returncode=0,
                stdout=json.dumps(agy_envelope, ensure_ascii=False).encode("utf-8"),
            )
        raise AssertionError(f"codex çağrılmamalıydı: {cmd[0]}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route("Rust async runtime farklarını araştıralım.")

    assert seen == ["claude", "agy"]
    assert decision.backend_used == "agy"
    assert action == "confirm"


async def test_all_backends_failing_raises_transient_error(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        if cmd[0] == "agy":
            return _FakeProcess(hang=True)
        if cmd[0] == "claude":
            return _FakeProcess(returncode=1, stderr=b"HTTP 429 rate limit exceeded")
        return _FakeProcess(returncode=1, stderr=b"network is unreachable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    cfg = SesliNotConfig()
    cfg.router.timeout_secs = 0.05
    router = IntentRouter(cfg)

    with pytest.raises(RouterTransientError) as exc_info:
        await router.route("PipeWire gecikmesini araştır.")

    msg = str(exc_info.value)
    assert "agy" in msg
    assert "claude" in msg
    assert "codex" in msg


# ---------------------------------------------------------------------------
# 7. İsteğe bağlı HTTP hızlı yolu (GEMINI_API_KEY / ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------


async def test_anthropic_http_fast_path_uses_header_and_skips_cli(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-never-leak-secret")
    captured_headers: dict[str, str] = {}
    captured_url: list[str] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
            captured_url.append(url)
            captured_headers.update(headers)
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "classify_intent",
                            "input": _sample_decision_dict(confidence=0.94),
                        }
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async def fail_cli(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("HTTP hızlı yolu başarılıyken CLI çağrılmamalı!")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_cli)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route("SQLite WAL davranışını araştır.")
    assert decision.backend_used == "anthropic_http"
    assert action == "auto_start"
    assert captured_headers.get("x-api-key") == "sk-test-never-leak-secret"
    assert "sk-test-never-leak-secret" not in captured_url[0]


async def test_gemini_http_429_falls_back_to_cli_without_leaking_key(
    clean_router_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_key = "AIza-fake-test-secret-key"
    monkeypatch.setenv("GEMINI_API_KEY", secret_key)

    class Fake429Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Fake429Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
            assert secret_key not in url
            assert headers.get("x-goog-api-key") == secret_key
            return httpx.Response(429, text="rate limit exceeded")

    monkeypatch.setattr(httpx, "AsyncClient", Fake429Client)

    async def fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        assert cmd[0] == "agy"
        return _FakeProcess(
            returncode=0,
            stdout=json.dumps(_sample_decision_dict(confidence=0.86)).encode("utf-8"),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    router = IntentRouter(SesliNotConfig())
    decision, action = await router.route("Wayland ekran paylaşımını araştır.")
    assert decision.backend_used == "agy"
    assert action == "auto_start"


# ---------------------------------------------------------------------------
# 8. Güven eşiği karar politikası (decide_action)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "is_research,confidence,intent_type,expected_action",
    [
        (True, 0.95, "imperative", "auto_start"),
        (True, 0.80, "imperative", "auto_start"),
        (True, 0.799, "imperative", "confirm"),
        (True, 0.70, "question_implied", "confirm"),
        (True, 0.55, "question_implied", "confirm"),
        (True, 0.549, "question_implied", "note_only"),
        (True, 0.10, "imperative", "note_only"),
        (False, 0.99, "narrative_mention", "note_only"),
        (False, 0.95, "negated", "note_only"),
        (False, 0.0, "note_only", "note_only"),
        # Fail-closed: is_research=True ama intent_type negated/narrative_mention
        (True, 0.95, "negated", "note_only"),
        (True, 0.95, "narrative_mention", "note_only"),
        (True, 0.95, "note_only", "note_only"),
    ],
)
def test_decide_action_thresholds(
    is_research: bool,
    confidence: float,
    intent_type: Any,
    expected_action: str,
) -> None:
    router = IntentRouter(SesliNotConfig())
    decision = RouteDecision(
        is_research=is_research,
        confidence=confidence,
        intent_type=intent_type,
        research_topic="Test konusu" if is_research else "",
    )
    assert router.decide_action(decision) == expected_action


def test_decide_action_respects_custom_config_thresholds() -> None:
    cfg = SesliNotConfig()
    cfg.router.auto_start_threshold = 0.90
    cfg.router.confirm_threshold = 0.60
    router = IntentRouter(cfg)

    d_85 = RouteDecision(is_research=True, confidence=0.85, intent_type="imperative")
    d_91 = RouteDecision(is_research=True, confidence=0.91, intent_type="imperative")
    d_58 = RouteDecision(is_research=True, confidence=0.58, intent_type="question_implied")

    assert router.decide_action(d_85) == "confirm"
    assert router.decide_action(d_91) == "auto_start"
    assert router.decide_action(d_58) == "note_only"

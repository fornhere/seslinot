"""Vault yazıcı, kasa-dışı FTS5 dizin ve Hafıza OS köprüsü birim testleri.

Tüm testler kesinlikle `tmp_path` içinde çalışır; kullanıcının gerçek
kasasına (`~/Obsidian/...`) veya `~/.config/` dizinine ASLA dokunmaz.
"""

from __future__ import annotations

import json
import os
import stat
import unicodedata
from pathlib import Path

import pytest

from seslinot.config import SesliNotConfig
from seslinot.models import (
    NoteIntent,
    NoteStatus,
    ResearchReportInput,
    SourceRecord,
    VerificationReport,
    VoiceNoteRecord,
)
from seslinot.vault import (
    HafizaBridge,
    VaultIndex,
    VaultWriter,
    atomic_write_text,
    is_referenced_in_hafiza_memory,
    safe_vault_path,
    scan_and_mask_secrets,
    turkish_lower,
    turkish_slug,
)


def _make_cfg(tmp_path: Path) -> SesliNotConfig:
    cfg = SesliNotConfig()
    cfg.vault.path = tmp_path / "kasa"
    cfg.vault.vault_name = "TestKasa"
    cfg.paths.data_dir = tmp_path / "data"
    cfg.paths.state_dir = tmp_path / "state"
    cfg.paths.runtime_dir = tmp_path / "run"
    cfg.paths.ensure_dirs()
    cfg.vault.path.mkdir(parents=True, exist_ok=True)
    return cfg


# --- Slug & Türkçe Küçük Harf ---


def test_turkish_slug_nfc_and_illegal_chars() -> None:
    nfd_input = unicodedata.normalize("NFD", "İSTANBUL'da Işık: Çekim/Ses? [Test] #1 *^|")
    slug = turkish_slug(nfd_input, max_words=6)
    assert unicodedata.is_normalized("NFC", slug)
    assert slug == "istanbulda-ışık-çekim-ses-test-1"
    for bad in r'\/:*?"<>|#^\[\]':
        assert bad not in slug

    assert turkish_slug("   ///???   ") == "not"
    assert turkish_slug("bir iki üç dört beş altı yedi sekiz dokuz", max_words=5) == (
        "bir-iki-üç-dört-beş"
    )
    assert turkish_lower("İSTANBUL IŞIK") == "istanbul ışık"


# --- Sır Tarama & Maskeleme ---


def test_secret_masking_and_idempotency() -> None:
    sample = "\n".join(
        [
            "OpenAI: sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "Anthropic: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234",
            "GitHub: ghp_1234567890abcdefghijklmnopqrstuvwxyzABCD",
            "AWS: AKIAIOSFODNN7EXAMPLE",
            "Auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
            "parola: cokGizli123",
            "şifre := superGizli456",
            "api_key=gizliDeger789",
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIEpAIBAAKCAQEAtokenbody",
            "-----END RSA PRIVATE KEY-----",
        ]
    )
    masked, rules = scan_and_mask_secrets(sample)
    assert "provider_sk" in rules
    assert "provider_github" in rules
    assert "provider_aws" in rules
    assert "bearer_token" in rules
    assert "legacy_assignment" in rules
    assert "private_key" in rules

    for leaked in (
        "sk-proj-",
        "sk-ant-",
        "ghp_1234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1Ni",
        "cokGizli123",
        "superGizli456",
        "gizliDeger789",
        "MIIEpAIBAAKCAQEAtokenbody",
    ):
        assert leaked not in masked

    # Idempotency: maskelenmiş metin tekrar tarandığında sıfır eşleşme vermeli
    masked_again, rules_again = scan_and_mask_secrets(masked)
    assert rules_again == []
    assert masked_again == masked


# --- Yol Güvenliği, Yasaklı Klasörler, Symlink Kaçışı ---


@pytest.mark.parametrize(
    "bad_rel",
    [
        "",
        "   ",
        "/etc/passwd",
        "sesli-notlar/../../etc/passwd",
        "../disari.md",
        "bilgi/yeni.md",
        "beyin/kavramlar/test.md",
        "zihin/hafıza-kataloğu.jsonl",
        "gelen-kutusu/ham.md",
        "günlük/2026-11-22.md",
        unicodedata.normalize("NFD", "günlük/2026-11-22.md"),
        ".obsidian/app.json",
        ".git/config",
        "araclar/hafiza.py",
        "sesli-notlar/.git/evil.md",
    ],
)
def test_safe_vault_path_rejects_forbidden_and_traversal(tmp_path: Path, bad_rel: str) -> None:
    vault = tmp_path / "kasa"
    vault.mkdir()
    with pytest.raises(ValueError):
        safe_vault_path(vault, bad_rel)


def test_safe_vault_path_rejects_symlink_escape_and_internal_symlink(tmp_path: Path) -> None:
    vault = tmp_path / "kasa"
    outside = tmp_path / "disari"
    vault.mkdir()
    outside.mkdir()
    (vault / "sesli-notlar").mkdir()

    # Dışarı işaret eden klasör symlink'i
    escape_dir = vault / "sesli-notlar" / "kacis"
    escape_dir.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        safe_vault_path(vault, "sesli-notlar/kacis/kotu.md")

    # Kasa içi dosya symlink'i
    real_file = vault / "sesli-notlar" / "gercek.md"
    real_file.write_text("merhaba", encoding="utf-8")
    link_file = vault / "sesli-notlar" / "bag.md"
    link_file.symlink_to(real_file)
    with pytest.raises(ValueError, match="Symlink"):
        safe_vault_path(vault, "sesli-notlar/bag.md")

    # Geçerli yol
    ok = safe_vault_path(vault, "sesli-notlar/2026-11/ornek.md")
    assert ok == (vault.resolve() / "sesli-notlar" / "2026-11" / "ornek.md")


def test_atomic_write_text_preserves_lock_and_cleans_temp(tmp_path: Path) -> None:
    target = tmp_path / "sesli-notlar" / "test.md"
    lock_path = tmp_path / "state" / "vault.lock"
    atomic_write_text(target, "Merhaba dünya\n", lock_path)

    assert target.read_text(encoding="utf-8") == "Merhaba dünya\n"
    assert lock_path.exists()  # Kilit dosyası asla silinmez
    leftovers = list(target.parent.glob(".seslinot-*.tmp"))
    assert leftovers == []


# --- VaultWriter: İskelet, Düz YAML, .base, Haritalar ---


def test_writer_scaffolding_voice_note_research_report_and_maps(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    writer = VaultWriter(cfg)

    created = writer.ensure_bank_scaffolding()
    assert len(created) == 4
    voice_readme = cfg.vault.path / "sesli-notlar" / "README.md"
    voice_base = cfg.vault.path / "sesli-notlar" / "Sesli Notlar.base"
    res_readme = cfg.vault.path / "araştırmalar" / "README.md"
    res_base = cfg.vault.path / "araştırmalar" / "Araştırmalar.base"

    assert voice_readme.is_file()
    assert voice_base.is_file()
    assert res_readme.is_file()
    assert res_base.is_file()
    assert 'tür == "sesli-not"' in voice_base.read_text(encoding="utf-8")
    assert 'tür == "araştırma"' in res_base.read_text(encoding="utf-8")

    # Kullanıcı tarafından düzenlenen README asla ezilmemeli
    voice_readme.write_text("# Özel Kullanıcı README\n", encoding="utf-8")
    assert writer.ensure_bank_scaffolding() == []
    assert voice_readme.read_text(encoding="utf-8") == "# Özel Kullanıcı README\n"

    # Sesli not yazımı (sır maskeleme + hub backlink engeli + harita yenileme)
    note = VoiceNoteRecord(
        note_id="sn-20261122-1905-a1b2",
        title="PipeWire Düşük Gecikme Ayarı",
        raw_transcript="PipeWire gecikmesini düşürmek için parola: gizli123 kullanma.",
        summary_bullets=["PipeWire quantum ayarı araştırılacak."],
        duration_secs=42,
        language="tr",
        stt_model="whisper-large-v3-turbo",
        scope="user",
        topic="linux-ses",
        concepts=["cekim"],
        tags=["sesli-not", "pipewire"],
        intent=NoteIntent.ARASTIRMA,
        status=NoteStatus.ISLENDI,
        created_at="2026-11-22T19:05:00+00:00",
    )
    abs_note, rel_note, masked_rules = writer.write_voice_note(
        note,
        wikilinks=[
            ("Ana Sayfa", "Ana Sayfa"),  # Hub backlink -> filtrelenmeli!
            ("sesli-notlar/Sesli Not Haritası", "Sesli Not Haritası"),  # Map -> filtrelenmeli!
            ("beyin/kavramlar/Çekim, kayıt ve ses", "Çekim, kayıt ve ses"),
        ],
    )
    assert abs_note.is_file()
    from datetime import datetime as _dt
    local_dt = _dt.fromisoformat(note.created_at).astimezone()
    expected_hhmm = local_dt.strftime("%H%M")
    expected_saat = local_dt.strftime("%H:%M")
    assert rel_note == f"sesli-notlar/2026-11/2026-11-22-{expected_hhmm}-pipewire-düşük-gecikme-ayarı.md"
    assert "legacy_assignment" in masked_rules

    note_text = abs_note.read_text(encoding="utf-8")
    assert "gizli123" not in note_text
    assert "Ana Sayfa" not in note_text
    assert "Sesli Not Haritası" not in note_text
    assert "[[beyin/kavramlar/Çekim, kayıt ve ses|Çekim, kayıt ve ses]]" in note_text
    for expected_field in (
        "tür: sesli-not",
        "kimlik: sn-20261122-1905-a1b2",
        "durum: işlendi",
        "niyet: araştırma",
        "tarih: 2026-11-22",
        f'saat: "{expected_saat}"',
        "süre_sn: 42",
        "dil: tr",
        "stt_modeli: whisper-large-v3-turbo",
        "kapsam: user",
        "konu: linux-ses",
        "kavramlar: [cekim]",
        "etiketler: [sesli-not, pipewire]",
        'ilgili_araştırma: ""',
        "hafıza_adayı_önerildi: false",
    ):
        assert expected_field in note_text
    assert "## Özet" in note_text
    assert "## Ham Döküm" in note_text
    assert "## İlgili Kasa Bağlantıları" in note_text

    # Harita kontrolü (tek-yönlü: harita -> not)
    voice_map = cfg.vault.path / "sesli-notlar" / "Sesli Not Haritası.md"
    assert voice_map.is_file()
    map_text = voice_map.read_text(encoding="utf-8")
    assert f"[[{rel_note[:-3]}|PipeWire Düşük Gecikme Ayarı]]" in map_text

    # Araştırma raporu yazımı
    report = ResearchReportInput(
        report_id="res-20261122-001",
        title="PipeWire Düşük Gecikme Rehberi",
        topic_slug="pipewire-düşük-gecikme",
        scope="user",
        topic="linux-ses",
        concepts=["cekim"],
        tags=["araştırma", "pipewire"],
        triggered_by_rel_path=rel_note,
        freshness_months=3,
        body_markdown="## Bulgular\n\nPipeWire quantum değeri 128 veya 256 seçilebilir [S01].",
        sources=[
            SourceRecord(
                source_id="S01",
                source_type="web",
                canonical_url="https://docs.pipewire.org/",
                final_url="https://docs.pipewire.org/",
                title="PipeWire Resmi Belgesi",
                fetch_status="fetched_ok",
                is_primary=True,
            ),
            SourceRecord(
                source_id="V01",
                source_type="vault",
                vault_path="beyin/kavramlar/Çekim, kayıt ve ses.md",
                title="Çekim, kayıt ve ses",
                fetch_status="vault_ok",
                is_primary=False,
            ),
        ],
        verification=VerificationReport(
            total_sources=2,
            verified_sources=2,
            quote_exact_match=3,
            quote_near_match=1,
            audit_status="passed",
        ),
        agent_count=3,
        duration_secs=95,
        confidence="yüksek",
        created_at="2026-11-22T19:15:00+00:00",
    )
    abs_rep, rel_rep, rep_masked = writer.write_research_report(report)
    assert abs_rep.is_file()
    assert rep_masked == []
    assert rel_rep == "araştırmalar/2026-11/2026-11-22-pipewire-düşük-gecikme.md"

    rep_text = abs_rep.read_text(encoding="utf-8")
    for expected_field in (
        "tür: araştırma",
        "kimlik: res-20261122-001",
        "durum: güncel",
        "tarih: 2026-11-22",
        "son_doğrulama: 2026-11-22",
        "tazelik_ay: 3",
        "kaynak_sayısı: 2",
        "birincil_kaynak_sayısı: 1",
        "doğrulanmış_alıntı: 4",
        "denetim_durumu: passed",
        "güven: yüksek",
        "ajan_sayısı: 3",
        "süre_sn: 95",
        f'tetik_notu: "[[{rel_note[:-3]}]]"',
        'yerine_geçen: ""',
    ):
        assert expected_field in rep_text
    assert "## Kaynaklar" in rep_text
    assert "## İlgili Kasa Bağlantıları" in rep_text

    res_map = cfg.vault.path / "araştırmalar" / "Araştırma Haritası.md"
    assert res_map.is_file()
    assert f"[[{rel_rep[:-3]}|PipeWire Düşük Gecikme Rehberi]]" in res_map.read_text(encoding="utf-8")


# --- Hash-Güvenlik Koruması (Katalog & bilgi/*.md) ---


def test_hash_safety_guard_for_voice_note_and_research_supersede(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    writer = VaultWriter(cfg)

    note = VoiceNoteRecord(
        note_id="sn-hash-1",
        title="Referanssız Sesli Not",
        raw_transcript="Mikrofon ayarı kontrol edilecek.",
        created_at="2026-11-22T10:00:00+00:00",
    )
    abs_note, rel_note, _ = writer.write_voice_note(note)

    rep_v1 = ResearchReportInput(
        report_id="res-v1",
        title="Mikrofon Rehberi V1",
        topic_slug="mikrofon-rehberi-v1",
        body_markdown="İlk sürüm.",
        created_at="2026-11-22T10:05:00+00:00",
    )
    abs_rep1, rel_rep1, _ = writer.write_research_report(rep_v1)

    # 1. Referanssız sesli nota araştırma bağlanabilir
    linked = writer.link_research_to_voice_note(rel_note, rel_rep1, "Mikrofon Rehberi V1")
    assert linked is True
    updated_note_text = abs_note.read_text(encoding="utf-8")
    assert "durum: araştırıldı" in updated_note_text
    assert f'ilgili_araştırma: "[[{rel_rep1[:-3]}]]"' in updated_note_text
    assert f"- [[{rel_rep1[:-3]}|Mikrofon Rehberi V1]]" in updated_note_text

    # Tekrar çağrıldığında satır çoğaltmamalı
    assert writer.link_research_to_voice_note(rel_note, rel_rep1, "Mikrofon Rehberi V1") is True
    assert updated_note_text.count(f"- [[{rel_rep1[:-3]}|Mikrofon Rehberi V1]]") == 1

    # 2. zihin/hafıza-kataloğu.jsonl içinde referanslı sesli nota DOKUNULMAMALI
    note_protected = VoiceNoteRecord(
        note_id="sn-hash-2",
        title="Katalogda Referanslı Not",
        raw_transcript="Bu not hafıza kataloğuna kaynak olarak bağlandı.",
        created_at="2026-11-22T11:00:00+00:00",
    )
    abs_prot, rel_prot, _ = writer.write_voice_note(note_protected)
    original_bytes = abs_prot.read_bytes()

    zihin_dir = cfg.vault.path / "zihin"
    zihin_dir.mkdir(parents=True, exist_ok=True)
    catalog_file = zihin_dir / "hafıza-kataloğu.jsonl"
    catalog_file.write_text(
        json.dumps(
            {
                "memory_id": "forn-test-1",
                "source_path": rel_prot,
                "source_hash": "sha256:deadbeef",
                "statement": "Örnek kalıcı hafıza kaydı",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    assert is_referenced_in_hafiza_memory(cfg.vault.path, rel_prot) is True
    assert writer.link_research_to_voice_note(rel_prot, rel_rep1, "Mikrofon Rehberi V1") is False
    assert abs_prot.read_bytes() == original_bytes  # Bayt düzeyinde değişmedi!

    # 3. bilgi/*.md içinde referanslı araştırma notu yerini-aldı yapılamaz
    bilgi_dir = cfg.vault.path / "bilgi"
    bilgi_dir.mkdir(parents=True, exist_ok=True)
    (bilgi_dir / "mikrofon-kurali.md").write_text(
        f"# Kural\n\n## Kaynaklar\n- [[{rel_rep1}]]\n",
        encoding="utf-8",
    )
    rep1_bytes = abs_rep1.read_bytes()
    assert (
        writer.mark_previous_research_superseded(
            rel_rep1, "araştırmalar/2026-11/2026-11-22-mikrofon-rehberi-v2.md"
        )
        is False
    )
    assert abs_rep1.read_bytes() == rep1_bytes

    # 4. Referanssız araştırma raporu yerini-aldı olarak işaretlenebilir
    rep_v2 = ResearchReportInput(
        report_id="res-v2",
        title="Mikrofon Rehberi V2",
        topic_slug="mikrofon-rehberi-v2",
        body_markdown="İkinci sürüm.",
        created_at="2026-11-22T12:00:00+00:00",
    )
    abs_rep2, rel_rep2, _ = writer.write_research_report(rep_v2)
    assert (
        writer.mark_previous_research_superseded(
            rel_rep2, "araştırmalar/2026-11/2026-11-22-mikrofon-rehberi-v3.md"
        )
        is True
    )
    rep2_text = abs_rep2.read_text(encoding="utf-8")
    assert "durum: yerini-aldı" in rep2_text
    assert 'yerine_geçen: "[[araştırmalar/2026-11/2026-11-22-mikrofon-rehberi-v3]]"' in rep2_text


# --- VaultIndex: Artımlı FTS5, Hariç Tutmalar, Türkçe Arama ---


def test_vault_index_incremental_sync_exclusions_secrets_and_turkish_search(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "kasa"
    vault.mkdir()
    fts_db = tmp_path / "data" / "vault_fts.sqlite"

    # Kasa içi FTS DB reddedilmeli
    with pytest.raises(ValueError):
        VaultIndex(vault, vault / "vault_fts.sqlite")

    # İndekslenebilir dosyalar
    (vault / "sesli-notlar" / "2026-11").mkdir(parents=True)
    note_a = vault / "sesli-notlar" / "2026-11" / "cekim-isik.md"
    note_a.write_text(
        "---\ntür: sesli-not\nkonu: çekim\n---\n# Çekim ve Işık Ayarı\n\n"
        "İstanbul'da düşük gecikme ve mikrofon kazancı ayarı yapıldı.\n",
        encoding="utf-8",
    )

    (vault / "araştırmalar" / "2026-11").mkdir(parents=True)
    note_b = vault / "araştırmalar" / "2026-11" / "pipewire.md"
    note_b.write_text(
        "# PipeWire Rehberi\n\n## Gecikme\n\nDüşük gecikmeli ses sunucusu ayarları.\n",
        encoding="utf-8",
    )

    # Sır içeren dosya atlanmalı
    secret_note = vault / "sesli-notlar" / "2026-11" / "gizli.md"
    secret_note.write_text(
        "# Gizli\n\nAnahtarım: sk-proj-abcdefghijklmnopqrstuvwxyz123456\n",
        encoding="utf-8",
    )

    # Navigasyon ve depo hub notları da FTS5 bağlamına girmemeli
    (vault / "Ana Sayfa.md").write_text("# Ana Sayfa\n\nDüşük gecikme ana sayfa.\n", encoding="utf-8")
    (vault / "sesli-notlar" / "README.md").write_text("# Sesli Notlar\n\nDüşük gecikme kuralı.\n", encoding="utf-8")
    (vault / "sesli-notlar" / "Sesli Not Haritası.md").write_text("# Harita\n\nDüşük gecikme haritası.\n", encoding="utf-8")
    (vault / "araştırmalar" / "Araştırma Haritası.md").write_text("# Harita\n\nDüşük gecikme araştırmaları.\n", encoding="utf-8")

    # Hariç tutulan dizinler
    for excluded in (
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
    ):
        d = vault / excluded
        d.mkdir(parents=True, exist_ok=True)
        (d / "haric.md").write_text("# Hariç\n\nMikrofon ışığı özel test metni.\n", encoding="utf-8")

    idx = VaultIndex(vault, fts_db)
    s1 = idx.sync()
    assert s1["added"] == 2
    assert s1["skipped_secret"] == 1
    assert s1["deleted"] == 0

    # İkinci senkronizasyon artımlı olmalı (değişmeyen 2)
    s2 = idx.sync()
    assert s2["added"] == 0
    assert s2["updated"] == 0
    assert s2["unchanged"] == 2

    # Türkçe diakritik & I/ı/İ aramaları
    for q in ("cekim", "çekim", "isik", "ışık", "dusuk", "düşük", "ayari", "ayarı"):
        hits = idx.search(q, limit=5)
        assert hits, f"Sorgu sonuç döndürmedi: {q}"
        assert hits[0].source_id == "V01"
        assert any("cekim-isik.md" in h.path for h in hits)

    # Klasör filtresi
    res_only = idx.search("düşük gecikme", folders=["araştırmalar"])
    assert len(res_only) == 1
    assert res_only[0].path == "araştırmalar/2026-11/pipewire.md"
    assert res_only[0].anchor == "Gecikme"

    # Güncelleme + silme artımlı senkronizasyonu
    note_a.write_text(
        "# Çekim ve Işık Ayarı V2\n\nYeni mikrofon filtresi eklendi.\n",
        encoding="utf-8",
    )
    os.utime(note_a, ns=(note_a.stat().st_atime_ns, note_a.stat().st_mtime_ns + 5_000_000))
    note_b.unlink()
    s3 = idx.sync()
    assert s3["updated"] == 1
    assert s3["deleted"] == 1
    assert idx.search("PipeWire") == []


# --- Kavram / Proje Eşleme ve Wikilink Keşfi ---


def test_concept_project_matching_and_verified_wikilink_discovery(tmp_path: Path) -> None:
    vault = tmp_path / "kasa"
    fts_db = tmp_path / "data" / "vault_fts.sqlite"
    (vault / "komuta").mkdir(parents=True)
    (vault / "beyin" / "kavramlar").mkdir(parents=True)
    (vault / "projeler" / "youtube").mkdir(parents=True)
    (vault / "sesli-notlar" / "2026-11").mkdir(parents=True)

    (vault / "komuta" / "kavramlar.json").write_text(
        json.dumps(
            {
                "kavramlar": [
                    {
                        "id": "kapak",
                        "title": "Kapak ve thumbnail",
                        "selectors": ["kapak", "thumbnail"],
                        "aliases": ["küçük resim", "okunurluk"],
                    },
                    {
                        "id": "cekim",
                        "title": "Çekim, kayıt ve ses",
                        "selectors": ["çekim", "mikrofon", "ışık"],
                        "aliases": ["gain", "gürültü"],
                    },
                    {
                        "id": "hayalet",
                        "title": "Olmayan Kavram Dosyası",
                        "selectors": ["hayalet"],
                        "aliases": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (vault / "komuta" / "gorev-baglam.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "youtube",
                        "aliases": ["kapak", "thumbnail", "video radarı"],
                        "status": "active",
                        "kind": "area",
                    },
                    {
                        "id": "gizli-proje",
                        "aliases": ["özel prototip"],
                        "status": "active",
                        "kind": "project",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Doğrulanmış hedef dosyalar
    (vault / "beyin" / "kavramlar" / "Kapak ve thumbnail.md").write_text(
        "# Kapak ve thumbnail\n", encoding="utf-8"
    )
    (vault / "beyin" / "kavramlar" / "Çekim, kayıt ve ses.md").write_text(
        "# Çekim, kayıt ve ses\n", encoding="utf-8"
    )
    # "Olmayan Kavram Dosyası.md" BİLEREK oluşturulmadı -> kırık link üretilmemeli!
    (vault / "projeler" / "youtube" / "DURUM.md").write_text(
        "# YouTube Durumu\n\nKapak ve thumbnail denemeleri sürüyor.\n", encoding="utf-8"
    )
    (vault / "sesli-notlar" / "2026-11" / "eski-kapak-notu.md").write_text(
        "# Eski Kapak Notu\n\nThumbnail okunurluğu ve mikrofon seviyesi.\n", encoding="utf-8"
    )
    # Hub/Harita dosyaları ASLA wikilink olarak dönmemeli
    (vault / "Ana Sayfa.md").write_text(
        "# Ana Sayfa\n\nKapak thumbnail mikrofon ışık.\n", encoding="utf-8"
    )
    (vault / "sesli-notlar" / "Sesli Not Haritası.md").write_text(
        "# Sesli Not Haritası\n\nKapak thumbnail mikrofon ışık.\n", encoding="utf-8"
    )

    idx = VaultIndex(vault, fts_db)
    idx.sync()

    # Türkçe İ/I küçük harf + 5 karakter kök öneki eşlemesi
    scope, concepts, projects, aliases = idx.match_concepts_and_projects(
        "IŞIK ve MİKROFONU düzeltip THUMBNAİLİ yenileyelim, hayalet testi"
    )
    assert scope == "project:youtube"
    assert "kapak" in concepts
    assert "cekim" in concepts
    assert "hayalet" in concepts
    assert projects == ["youtube"]
    assert "küçük resim" in aliases
    assert "gain" in aliases

    # Wikilink keşfi: kırık link yok, hub/harita backlink yok, tüm hedefler is_file()
    links = idx.discover_wikilinks(
        "Işık, mikrofon ve thumbnail kapağı üzerine hayalet testi",
        exclude_rel_path="sesli-notlar/2026-11/yeni-not.md",
        limit=5,
    )
    targets = [t for t, _ in links]
    assert len(links) >= 3
    assert "beyin/kavramlar/Kapak ve thumbnail" in targets
    assert "beyin/kavramlar/Çekim, kayıt ve ses" in targets
    assert "projeler/youtube/DURUM" in targets
    assert "beyin/kavramlar/Olmayan Kavram Dosyası" not in targets
    assert "Ana Sayfa" not in targets
    assert "sesli-notlar/Sesli Not Haritası" not in targets

    for target_no_md, _ in links:
        assert (vault / f"{target_no_md}.md").is_file()


# --- HafizaBridge: Bağlam Toplama, Gizlilik Duvarı, Onaylı Terfi ---


def test_hafiza_bridge_generic_vault_and_privacy_firewall(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    (cfg.vault.path / "komuta").mkdir(parents=True)
    (cfg.vault.path / "sesli-notlar" / "2026-11").mkdir(parents=True)
    (cfg.vault.path / "sesli-notlar" / "2026-11" / "not.md").write_text(
        "# Hyprland Wayland Notu\n\nWayland ekran paylaşımı için PipeWire kullan.\n",
        encoding="utf-8",
    )
    (cfg.vault.path / "komuta" / "gorev-baglam.json").write_text(
        json.dumps(
            {
                "projects": [
                    {"id": "youtube", "status": "active", "kind": "area", "aliases": []},
                    {"id": "gizli-kodadi", "status": "active", "kind": "project", "aliases": []},
                    {"id": "eski-proje", "status": "archived", "kind": "project", "aliases": []},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (cfg.vault.path / "komuta" / "yayin-yasak-terimler.txt").write_text(
        "# Yasaklı terimler\nIcProjeX\n",
        encoding="utf-8",
    )

    bridge = HafizaBridge(cfg)

    # araclar/hafiza.py olmadan sorunsuz FTS5 bağlamı döndürmeli
    bundle = bridge.gather_vault_context("Hyprland PipeWire ekran paylaşımı", limit=4)
    assert bundle.scope == "user"
    assert bundle.catalog_text == ""
    assert len(bundle.hits) == 1
    assert bundle.hits[0].source_id == "V01"
    assert bundle.hits[0].path == "sesli-notlar/2026-11/not.md"

    # Gizlilik Duvarı (Privacy Firewall)
    cfg.vault.personal_terms = ["dorn", "dornhere"]
    dirty_query = (
        "Dorn'un /home/alice/Vault/projeler/gizli-kodadi/not.md dosyasındaki "
        "gizli-kodadi, OzelMusteri ve IcProjeX için dornhere YouTube Adorned PipeWire "
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456 ayarları"
    )
    sanitized = bridge.sanitize_web_query(dirty_query, private_terms=["OzelMusteri"])
    for forbidden in (
        "Dorn",
        "dornhere",
        "/home/alice",
        "gizli-kodadi",
        "OzelMusteri",
        "IcProjeX",
        "sk-proj-",
    ):
        assert forbidden not in sanitized
    assert "dorn" not in sanitized.replace("Adorned", "").lower()
    assert "YouTube" in sanitized
    assert "Adorned" in sanitized
    assert "PipeWire" in sanitized


def test_hafiza_bridge_mock_cli_context_and_explicit_promotion(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    tools_dir = cfg.vault.path / "araclar"
    tools_dir.mkdir(parents=True)
    log_file = tmp_path / "hafiza_cli_calls.jsonl"

    mock_script = tools_dir / "hafiza.py"
    mock_script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

log_path = Path({str(log_file)!r})
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "jev": os.environ.get("HAFIZA_HOOK_JEV")}}) + "\\n")

args = sys.argv[1:]
if "context" in args:
    has_uri = "--uri" in args
    if not has_uri:
        print(json.dumps({{
            "text": "Kanonik katalog: [mem-1] Kısa özet tercih edilir.",
            "notes": [
                {{"path": "bilgi/ozet-tercihi.md", "anchor": "Özet", "score": 0.92, "excerpt": "Kısa özet kuralı."}}
            ]
        }}, ensure_ascii=False))
    else:
        print(json.dumps({{
            "text": "URI taraması",
            "notes": [
                {{"path": "bilgi/ozet-tercihi.md", "anchor": "Özet", "score": 0.90, "excerpt": "Tekrar eden kayıt."}},
                {{"path": "görüşler/kaynaklı/ornek.md", "anchor": "", "score": 0.75, "excerpt": "Görüş alıntısı."}}
            ]
        }}, ensure_ascii=False))
    sys.exit(0)

if "candidate-add" in args:
    print(json.dumps({{"result": "queued", "candidate_id": "cand-001"}}))
    sys.exit(0)

if "candidate-assess" in args:
    verdict = os.environ.get("MOCK_ASSESS_RESULT", "eligible")
    print(json.dumps({{"result": verdict}}))
    sys.exit(0)

if "promote" in args:
    print(json.dumps({{"result": "promoted", "applied": "--apply" in args}}))
    sys.exit(0)

sys.exit(2)
""",
        encoding="utf-8",
    )
    mock_script.chmod(mock_script.stat().st_mode | stat.S_IEXEC)

    # Bir de FTS5 notu koyalım
    (cfg.vault.path / "sesli-notlar" / "2026-11").mkdir(parents=True)
    (cfg.vault.path / "sesli-notlar" / "2026-11" / "ozet-notu.md").write_text(
        "# Özet Notu\n\nKısa özet yaklaşımı.\n", encoding="utf-8"
    )

    bridge = HafizaBridge(cfg)
    bundle = bridge.gather_vault_context("kısa özet yaklaşımı", limit=6)

    assert "Kanonik katalog" in bundle.catalog_text
    paths = [h.path for h in bundle.hits]
    assert "bilgi/ozet-tercihi.md" in paths
    assert "görüşler/kaynaklı/ornek.md" in paths
    assert "sesli-notlar/2026-11/ozet-notu.md" in paths
    assert len(paths) == len(set(paths))  # Tekilleştirilmiş
    assert [h.source_id for h in bundle.hits] == [f"V{i:02d}" for i in range(1, len(bundle.hits) + 1)]

    # CLI çağrı sözleşmesini doğrula (HAFIZA_HOOK_JEV=0, --local, 1A ve 1B)
    calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert all(c["jev"] == "0" for c in calls)
    assert "--local" in calls[0]["argv"]
    assert "--uri" not in calls[0]["argv"]
    assert "--uri" in calls[1]["argv"]
    assert "araştırmalar" in calls[1]["argv"]
    assert "sesli-notlar" in calls[1]["argv"]

    # Onaysız veya sırlı terfi reddedilmeli
    with pytest.raises(ValueError):
        bridge.promote_user_approved_memory(
            statement="Kısa özet tercih edilir.",
            subject_key="stil.ozet",
            source_path="sesli-notlar/2026-11/ozet-notu.md",
            user_approved=False,
        )
    with pytest.raises(ValueError):
        bridge.promote_user_approved_memory(
            statement="parola: gizli123",
            subject_key="stil.ozet",
            source_path="sesli-notlar/2026-11/ozet-notu.md",
            user_approved=True,
        )

    # Açık kullanıcı onaylı terfi akışı (candidate-add -> candidate-assess -> promote --apply)
    promo = bridge.promote_user_approved_memory(
        statement="Kullanıcı sesli not özetlerinin en fazla 3 madde olmasını tercih eder.",
        subject_key="seslinot.ozet.uzunluk",
        source_path="sesli-notlar/2026-11/ozet-notu.md",
        memory_id="seslinot-ozet-3-madde",
        reviewed_by="reviewer1",
        user_approved=True,
    )
    assert promo["ok"] is True
    assert promo["promoted"] is True
    assert promo["candidate_id"] == "cand-001"
    assert promo["memory_id"] == "seslinot-ozet-3-madde"

    calls_after = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    promo_calls = calls_after[2:]
    assert "candidate-add" in promo_calls[0]["argv"]
    assert "--proposed-by" in promo_calls[0]["argv"]
    assert "seslinot" in promo_calls[0]["argv"]
    assert "candidate-assess" in promo_calls[1]["argv"]
    assert "promote" in promo_calls[2]["argv"]
    assert "--reviewed-by" in promo_calls[2]["argv"]
    assert "reviewer1" in promo_calls[2]["argv"]
    assert "--apply" in promo_calls[2]["argv"]

    # Eligible olmayan aday (duplicate) promote çağırmamalı
    os.environ["MOCK_ASSESS_RESULT"] = "duplicate"
    try:
        dup = bridge.promote_user_approved_memory(
            statement="Tekrarlanan tercih.",
            subject_key="seslinot.ozet.uzunluk",
            source_path="sesli-notlar/2026-11/ozet-notu.md",
            user_approved=True,
        )
    finally:
        os.environ.pop("MOCK_ASSESS_RESULT", None)
    assert dup["ok"] is False
    assert dup["promoted"] is False
    assert dup["stage"] == "candidate-assess"


def test_same_minute_voice_note_collision_and_flat_yaml_guard(tmp_path: Path) -> None:
    from seslinot.vault.writer import _render_flat_yaml

    with pytest.raises(ValueError, match="İç içe YAML"):
        _render_flat_yaml([("gecersiz", {"alt": "nesne"})])
    with pytest.raises(ValueError, match="İç içe YAML"):
        _render_flat_yaml([("gecersiz_liste", [{"a": 1}])])

    cfg = _make_cfg(tmp_path)
    writer = VaultWriter(cfg)
    n1 = VoiceNoteRecord(
        note_id="sn-20261122-1905-aaaa",
        title="Aynı Başlık",
        raw_transcript="Birinci kayıt.",
        created_at="2026-11-22T19:05:00+00:00",
    )
    n2 = VoiceNoteRecord(
        note_id="sn-20261122-1905-bbbb",
        title="Aynı Başlık",
        raw_transcript="İkinci kayıt.",
        created_at="2026-11-22T19:05:30+00:00",
    )
    p1, rel1, _ = writer.write_voice_note(n1)
    p2, rel2, _ = writer.write_voice_note(n2)
    assert p1 != p2
    assert rel1 != rel2
    assert "Birinci kayıt." in p1.read_text(encoding="utf-8")
    assert "İkinci kayıt." in p2.read_text(encoding="utf-8")


def test_hafiza_bridge_accepts_rc1_context_json(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    tools_dir = cfg.vault.path / "araclar"
    tools_dir.mkdir(parents=True)
    mock_script = tools_dir / "hafiza.py"
    mock_script.write_text(
        """#!/usr/bin/env python3
import json, sys
print(json.dumps({
    "text": "Uyarıyla dönen katalog bağlamı",
    "notes": [{"path": "bilgi/ornek.md", "anchor": "A", "score": 0.8, "excerpt": "Bilgi metni"}],
    "catalog_errors": ["minor-warning"]
}, ensure_ascii=False))
sys.exit(1)
""",
        encoding="utf-8",
    )
    mock_script.chmod(mock_script.stat().st_mode | stat.S_IEXEC)

    bridge = HafizaBridge(cfg)
    bundle = bridge.gather_vault_context("örnek sorgu", limit=4)
    assert "Uyarıyla dönen katalog bağlamı" in bundle.catalog_text
    assert len(bundle.hits) == 1
    assert bundle.hits[0].kind == "bilgi"
    assert bundle.hits[0].path == "bilgi/ornek.md"


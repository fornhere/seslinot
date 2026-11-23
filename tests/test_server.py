"""SesliNot lokal HTTP + SSE API sunucusu ve secrets.env testleri."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from seslinot.config import (
    DesktopConfig,
    PathsConfig,
    SesliNotConfig,
    VaultConfig,
    load_config,
    save_secrets_env,
)
from seslinot.models import JobStatus, ResearchJob, ResearchPhase, ResearchReportInput, SourceRecord
from seslinot.server import create_app


def _make_cfg(tmp_path: Path) -> SesliNotConfig:
    vault = tmp_path / "Vault"
    vault.mkdir(parents=True, exist_ok=True)
    return SesliNotConfig(
        vault=VaultConfig(path=vault, vault_name="Vault"),
        paths=PathsConfig(
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "runtime",
        ),
        desktop=DesktopConfig(enable_osd=False, enable_notifications=False, enable_bar_refresh=False),
    )


@pytest.mark.asyncio
async def test_server_auth_notes_approval_and_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    cfg_path = tmp_path / "cfg" / "config.toml"
    token = "test-secret-token-123"
    app = create_app(cfg, config_path=cfg_path, api_token=token, start_daemon=False)

    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        # 1) Health herkese açık, state yetki gerektirir
        h = await client.get("/api/health")
        assert h.status_code == 200
        assert h.json()["ok"] is True

        unauth = await client.get("/api/state")
        assert unauth.status_code == 401

        st = await client.get("/api/state", headers=headers)
        assert st.status_code == 200
        assert st.json()["needsOnboarding"] is True

        # 2) Onboarding tamamlama -> config.toml + secrets.env + kasa iskeleti
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        ob = await client.post(
            "/api/onboarding/complete",
            headers=headers,
            json={
                "vaultPath": str(cfg.vault.path),
                "sttProvider": "groq",
                "researchMode": "api",
                "apiProvider": "openai",
                "apiModel": "gpt-4o-mini",
                "requireSaveApproval": True,
                "secrets": {"GROQ_API_KEY": "gsk_demo_999"},
            },
        )
        assert ob.status_code == 200
        assert ob.json()["ok"] is True
        assert cfg_path.is_file()
        assert (cfg_path.parent / "secrets.env").is_file()
        assert ob.json()["settings"]["secrets"]["GROQ_API_KEY"] is True
        # Anahtar değeri asla GET yanıtında dönmemeli
        assert "gsk_demo_999" not in json.dumps(ob.json())

        # 3) Metin notu oluşturma
        note_res = await client.post(
            "/api/notes",
            headers=headers,
            json={"text": "Yarın yeni arayüzü kaydedeceğiz."},
        )
        assert note_res.status_code == 200
        assert note_res.json()["vaultRelPath"].startswith("sesli-notlar/")

        # 4) AWAITING_APPROVAL işi için önizleme + onaylayıp kasaya kaydetme
        state_obj = app.state.seslinot
        sess_dir = state_obj.cfg.paths.sessions_dir / "job-ui-1"
        sess_dir.mkdir(parents=True, exist_ok=True)
        preview_path = sess_dir / "preview.md"
        preview_path.write_text("# [ONAY BEKLİYOR] Test Rapor\n\nÖzet içerik.\n", encoding="utf-8")

        report = ResearchReportInput(
            report_id="rep-ui-1",
            title="Test Rapor",
            topic_slug="test-rapor",
            body_markdown="SQLite WAL hızlıdır.[^1]\n\n## Kaynaklar\n\n[^1]: [SQLite](https://sqlite.org/wal.html)\n",
            confidence="yüksek",
            sources=[
                SourceRecord(
                    source_id="S01",
                    source_type="web",
                    canonical_url="https://sqlite.org/wal.html",
                    final_url="https://sqlite.org/wal.html",
                    title="SQLite WAL",
                    fetch_status="fetched_ok",
                )
            ],
        )
        (sess_dir / "pending_report.json").write_text(
            json.dumps(
                {
                    "job_id": "job-ui-1",
                    "note_id": note_res.json()["noteId"],
                    "triggered_by_rel_path": note_res.json()["vaultRelPath"],
                    "preview_path": str(preview_path),
                    "summary": "SQLite WAL hızlıdır.",
                    "source_count": 1,
                    "verified_quotes": 1,
                    "confidence": "yüksek",
                    "audit_status": "passed",
                    "report": report.model_dump(mode="json"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state_obj.pipeline.db.enqueue_job(
            ResearchJob(
                job_id="job-ui-1",
                note_id=note_res.json()["noteId"],
                topic="SQLite WAL",
                raw_prompt="SQLite WAL araştır",
                status=JobStatus.AWAITING_APPROVAL,
                phase=ResearchPhase.AWAITING_APPROVAL,
                session_dir=str(sess_dir),
                source_count=1,
            )
        )

        prev = await client.get("/api/jobs/job-ui-1/preview", headers=headers)
        assert prev.status_code == 200
        assert "[ONAY BEKLİYOR]" in prev.json()["markdown"]
        assert prev.json()["meta"]["sourceCount"] == 1

        appr = await client.post("/api/jobs/job-ui-1/approve-report", headers=headers)
        assert appr.status_code == 200
        assert appr.json()["ok"] is True
        assert appr.json()["reportRelPath"].startswith("araştırmalar/")
        assert (state_obj.cfg.vault.path / appr.json()["reportRelPath"]).is_file()

        # 5) Doctor ve Settings GET/PUT
        doc = await client.get("/api/doctor", headers=headers)
        assert doc.status_code == 200
        assert doc.json()["doctor"]["vaultExists"] is True

        upd = await client.put(
            "/api/settings",
            headers=headers,
            json={"research": {"require_save_approval": False}},
        )
        assert upd.status_code == 200
        assert upd.json()["settings"]["research"]["require_save_approval"] is False
        reloaded = load_config(cfg_path)
        assert reloaded.research.require_save_approval is False


def test_secrets_env_roundtrip_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    res = save_secrets_env({"OPENAI_API_KEY": "sk-local-test-123"}, cfg_path)
    assert res["OPENAI_API_KEY"] is True
    assert (cfg_path.parent / "secrets.env").is_file()

    # Ortamdan silip load_config ile geri yüklendiğini doğrula
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    load_config(cfg_path)
    import os

    assert os.environ.get("OPENAI_API_KEY") == "sk-local-test-123"

    # Boş dize ile temizleme
    res2 = save_secrets_env({"OPENAI_API_KEY": ""}, cfg_path)
    assert res2["OPENAI_API_KEY"] is False

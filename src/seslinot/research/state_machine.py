"""Model 2 ince Python durum makinesi: 4+1 aşamalı çok-ajanlı derin araştırma orkestratörü."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seslinot.config import SesliNotConfig
from seslinot.models import (
    PHASE_LABELS_TR,
    QuoteVerification,
    ResearchJob,
    ResearchPhase,
    ResearchPlan,
    ResearchReportInput,
    SourceRecord,
    SubagentDirective,
    SubagentOutput,
    VaultContextBundle,
    utc_now_iso,
)
from seslinot.research.adapters.base import (
    WorkerAdapter,
    WorkerRequest,
    extract_snippets_from_stream,
    extract_urls_from_stream,
    get_adapter,
)
from seslinot.research.audit import CitationAuditor
from seslinot.research.registry import FetchFunc, SourceRegistry
from seslinot.research.runner import ProcessRunner
from seslinot.research.verifier import render_deterministic_report, verify_all_findings

ProgressCallback = Callable[[ResearchPhase, str], Any]

_PRIVATE_PATH_RE = re.compile(r"(?:/home/[^\s\"']+|[A-Za-z]:\\[^\s\"']+|~/[^\s\"']+)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET_TOKEN_RE = re.compile(r"\b(?:sk|ghp|gho|xoxb|AIza)[A-Za-z0-9_-]{12,}\b")
_MD_REL_PATH_RE = re.compile(r"\b[\w./-]+\.md\b", re.IGNORECASE)
_SLUG_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def _slugify(text: str) -> str:
    cleaned = text.translate(_SLUG_TR_MAP).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return cleaned[:64] or "arastirma"


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def sanitize_web_queries(
    plan: ResearchPlan,
    vault_context: VaultContextBundle | None,
    vault_path: Path,
) -> ResearchPlan:
    """Web arama sorgularından yerel dosya yollarını, kasa adlarını ve özel terimleri temizler."""
    private_terms: list[str] = []
    if vault_path.name and len(vault_path.name) >= 3:
        private_terms.append(vault_path.name)
    private_terms.append(str(vault_path))
    if vault_context is not None:
        for hit in vault_context.hits:
            if hit.path:
                private_terms.append(hit.path)
                stem = Path(hit.path).stem
                if len(stem) >= 4:
                    private_terms.append(stem)

    def _clean_query(q: str) -> str:
        s = _PRIVATE_PATH_RE.sub(" ", q)
        s = _EMAIL_RE.sub(" ", s)
        s = _SECRET_TOKEN_RE.sub(" ", s)
        s = _MD_REL_PATH_RE.sub(" ", s)
        for term in private_terms:
            if term and len(term) >= 3:
                s = re.sub(re.escape(term), " ", s, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", s).strip()

    for directive in plan.directives:
        directive.queries_en = [cq for q in directive.queries_en if (cq := _clean_query(q))]
        directive.queries_tr = [cq for q in directive.queries_tr if (cq := _clean_query(q))]

    return plan


class ResearchOrchestrator:
    """4+1 aşamalı (Plan -> Paralel Subagent -> 0-Token Doğrulama -> Sentez -> Denetim) orkestratör."""

    def __init__(
        self,
        cfg: SesliNotConfig,
        *,
        adapter_factory: Callable[[str], WorkerAdapter] = get_adapter,
        runner: ProcessRunner | None = None,
        fetcher: FetchFunc | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self.adapter_factory = adapter_factory
        self.runner = runner or ProcessRunner(use_systemd_run=cfg.research.use_systemd_run)
        self.fetcher = fetcher
        self.progress_cb = progress_cb
        self._state_lock = asyncio.Lock()

    async def _emit_progress(self, phase: ResearchPhase, detail: str = "") -> None:
        if self.progress_cb is None:
            return
        msg = detail or PHASE_LABELS_TR.get(phase, phase.value)
        res = self.progress_cb(phase, msg)
        if inspect.isawaitable(res):
            await res

    def _resolve_session_dir(self, job: ResearchJob, session_dir: Path | None) -> Path:
        if session_dir is not None:
            return Path(session_dir)
        if job.session_dir:
            existing = Path(job.session_dir)
            if existing.exists():
                return existing
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job.job_id).strip("-") or "job"
        return self.cfg.paths.sessions_dir / f"{stamp}-{safe_id}"

    def _load_state(self, state_path: Path, job: ResearchJob) -> dict[str, Any]:
        if state_path.is_file():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "job_id": job.job_id,
            "topic": job.topic,
            "phase": ResearchPhase.VAULT_SCAN.value,
            "completed_phases": [],
            "completed_workers": [],
            "failed_workers": [],
            "updated_at": utc_now_iso(),
        }

    async def _save_state(
        self,
        state_path: Path,
        state: dict[str, Any],
        *,
        phase: ResearchPhase | None = None,
        completed_phase: str | None = None,
        completed_worker: str | None = None,
        failed_worker: str | None = None,
    ) -> None:
        async with self._state_lock:
            if phase is not None:
                state["phase"] = phase.value
            if completed_phase and completed_phase not in state["completed_phases"]:
                state["completed_phases"].append(completed_phase)
            if completed_worker and completed_worker not in state["completed_workers"]:
                state["completed_workers"].append(completed_worker)
                if completed_worker in state["failed_workers"]:
                    state["failed_workers"].remove(completed_worker)
            if failed_worker and failed_worker not in state["failed_workers"]:
                state["failed_workers"].append(failed_worker)
            state["updated_at"] = utc_now_iso()
            _write_json_atomic(state_path, state)

    # --- Phase 1: Lead Agent ---

    def _build_lead_prompts(
        self,
        job: ResearchJob,
        vault_context: VaultContextBundle,
        max_workers: int,
    ) -> tuple[str, str]:
        system_prompt = (
            "Sen çok-ajanlı bir derin araştırma sisteminin Baş Araştırmacısısın (Lead Agent). "
            "Kullanıcının Türkçe sesli notunu ve yerel Hafıza kasası bağlamını analiz edip "
            f"en fazla {max_workers} adet birbirinden bağımsız, çakışmayan alt araştırma görevi "
            "(SubagentDirective) planla.\n"
            "KRİTİK GİZLİLİK KURALI: queries_en ve queries_tr içine ASLA yerel dosya yolu, "
            "kullanıcı adı, kasa dosya adı, e-posta veya özel kişisel bilgi koyma.\n"
            "Yanıtını YALNIZCA ResearchPlan JSON şemasına uygun olarak üret."
        )
        vault_lines: list[str] = []
        for hit in vault_context.hits[:8]:
            excerpt = hit.excerpt[:300].replace("\n", " ")
            vault_lines.append(f"- [{hit.source_id}] {hit.title or hit.path}: {excerpt}")
        vault_summary = "\n".join(vault_lines) if vault_lines else "(Kasa eşleşmesi yok)"

        user_prompt = (
            f"Konu: {job.topic}\n"
            f"Derinlik: {job.depth}\n"
            f"Hedef Alt Ajan Sayısı: en fazla {max_workers}\n"
            f"Varsayılan Güncellik Penceresi: {self.cfg.research.default_freshness_months} ay\n\n"
            f"Kullanıcı Ham Transkripti:\n{job.raw_prompt}\n\n"
            f"Kasa Kavramları: {', '.join(vault_context.matched_concepts) or '-'}\n"
            f"Kasa Projeleri: {', '.join(vault_context.matched_projects) or '-'}\n"
            f"İlgili Kasa Bağlamı:\n{vault_summary}\n"
        )
        return system_prompt, user_prompt

    def _write_directives(self, directives_dir: Path, plan: ResearchPlan) -> None:
        directives_dir.mkdir(parents=True, exist_ok=True)
        (directives_dir / "plan.json").write_text(
            plan.model_dump_json(indent=2),
            encoding="utf-8",
        )
        questions_md = "\n".join(f"- {q}" for q in plan.key_questions) or "- (Belirtilmedi)"
        brief_md = (
            f"# {plan.title_tr}\n\n"
            f"{plan.brief_summary}\n\n"
            f"## Temel Sorular\n\n{questions_md}\n"
        )
        (directives_dir / "brief.md").write_text(brief_md, encoding="utf-8")

        for directive in plan.directives:
            prompt_md = (
                f"# Alt Ajan Görevi: {directive.worker_id}\n\n"
                f"**Alt Konu:** {directive.subtopic}\n\n"
                f"**Hedef:** {directive.objective}\n\n"
                f"**Kapsam Dışı:** {directive.out_of_scope or 'Diğer alt ajanların sorumluluk alanları'}\n\n"
                f"**İngilizce Arama Sorguları:**\n"
                + "\n".join(f"- {q}" for q in directive.queries_en)
                + "\n\n**Türkçe Arama Sorguları:**\n"
                + "\n".join(f"- {q}" for q in directive.queries_tr)
                + "\n\n**Kaynak Odak Türleri:** "
                + ", ".join(directive.focus_source_types)
                + "\n\n**Çıktı Kuralları:**\n"
                "- Her bulgu için kaynaktan birebir değiştirilmemiş `exact_quote` kopyala.\n"
                "- Her web bulgusu için gerçek ve tam `url` ver; uydurma URL yazma.\n"
                "- `claim_tr` alanını net, tarafsız Türkçe olarak yaz.\n"
                "- Yalnızca SubagentOutput JSON şemasında yanıt ver.\n"
            )
            (directives_dir / f"{directive.worker_id}.prompt.md").write_text(
                prompt_md,
                encoding="utf-8",
            )

    async def _run_phase_direction(
        self,
        job: ResearchJob,
        vault_context: VaultContextBundle,
        session_dir: Path,
        state: dict[str, Any],
        state_path: Path,
    ) -> ResearchPlan:
        directives_dir = session_dir / "directives"
        plan_path = directives_dir / "plan.json"

        # Crash-resume: plan zaten diskteyse Lead çağrısını atla
        if plan_path.is_file():
            try:
                loaded_plan = ResearchPlan.model_validate_json(
                    plan_path.read_text(encoding="utf-8")
                )
                if loaded_plan.directives:
                    # Frozen prompt dosyaları eksikse tamamla
                    for d in loaded_plan.directives:
                        if not (directives_dir / f"{d.worker_id}.prompt.md").is_file():
                            self._write_directives(directives_dir, loaded_plan)
                            break
                    return loaded_plan
            except Exception:
                pass

        await self._emit_progress(ResearchPhase.DIRECTION)
        max_workers = (
            min(2, self.cfg.research.max_subagents)
            if job.depth == "quick"
            else max(1, self.cfg.research.max_subagents)
        )
        system_prompt, user_prompt = self._build_lead_prompts(job, vault_context, max_workers)
        schema = ResearchPlan.model_json_schema()

        lead_candidates: list[tuple[str, str]] = [
            (self.cfg.research.lead_backend, self.cfg.research.lead_model)
        ]
        if self.cfg.research.worker_fallback_backend != self.cfg.research.lead_backend:
            lead_candidates.append(
                (self.cfg.research.worker_fallback_backend, self.cfg.research.worker_fallback_model)
            )

        plan: ResearchPlan | None = None
        last_error = ""
        for idx, (backend, model) in enumerate(lead_candidates):
            lead_dir = session_dir / ("lead" if idx == 0 else f"lead_fallback_{backend}")
            adapter = self.adapter_factory(backend)
            req = WorkerRequest(
                worker_id="lead",
                role="lead",
                prompt=user_prompt,
                system_prompt=system_prompt,
                work_dir=lead_dir,
                json_schema=schema,
                model=model,
                allow_web=False,
                timeout_secs=min(180, self.cfg.research.worker_timeout_secs),
                max_budget_usd=self.cfg.research.max_budget_usd,
                unit_name=f"seslinot-lead-{job.job_id}",
            )
            res = await self.runner.run(adapter, req)
            if res.ok and res.parsed_json:
                try:
                    plan = ResearchPlan.model_validate(res.parsed_json)
                    break
                except Exception as exc:
                    last_error = f"ResearchPlan doğrulama hatası: {exc}"
            else:
                last_error = res.error or "Lead ajanı başarısız oldu"

        if plan is None:
            raise RuntimeError(f"Faz 1 (Yön Tayini) başarısız: {last_error}")

        # Yönerge sayısını ve worker_id benzersizliğini normalize et
        if not plan.directives:
            plan.directives = [
                SubagentDirective(
                    worker_id="worker_01",
                    subtopic=job.topic,
                    objective=f"{job.topic} hakkında güncel ve birincil kaynakları topla.",
                    queries_en=[job.topic],
                    queries_tr=[job.topic],
                )
            ]
        plan.directives = plan.directives[:max_workers]
        used_ids: set[str] = set()
        for idx, d in enumerate(plan.directives, start=1):
            wid = re.sub(r"[^A-Za-z0-9_-]+", "_", d.worker_id.strip())
            if not wid or wid in used_ids:
                wid = f"worker_{idx:02d}"
            d.worker_id = wid
            used_ids.add(wid)

        if not plan.topic_slug:
            plan.topic_slug = _slugify(plan.title_tr or job.topic)

        plan = sanitize_web_queries(plan, vault_context, self.cfg.vault.path)
        self._write_directives(directives_dir, plan)
        await self._save_state(
            state_path,
            state,
            phase=ResearchPhase.DIRECTION,
            completed_phase=ResearchPhase.DIRECTION.value,
        )
        return plan

    # --- Phase 2: Parallel Subagents ---

    async def _run_single_subagent(
        self,
        job: ResearchJob,
        directive: SubagentDirective,
        session_dir: Path,
        sem: asyncio.Semaphore,
        state: dict[str, Any],
        state_path: Path,
    ) -> tuple[SubagentOutput, list[str], dict[str, list[str]]]:
        worker_dir = session_dir / "workers" / directive.worker_id
        findings_path = worker_dir / "findings.json"
        observed_path = worker_dir / "observed_urls.json"

        # Crash-resume: tamamlanmış worker findings.json diskteyse yeniden çalıştırma!
        if findings_path.is_file():
            try:
                cached = SubagentOutput.model_validate_json(
                    findings_path.read_text(encoding="utf-8")
                )
                if cached.status != "failed":
                    obs_urls: list[str] = []
                    if observed_path.is_file():
                        obs_urls = json.loads(observed_path.read_text(encoding="utf-8"))
                    elif (worker_dir / "raw_stream.jsonl").is_file():
                        obs_urls = extract_urls_from_stream(worker_dir / "raw_stream.jsonl")
                    snippets = extract_snippets_from_stream(worker_dir / "raw_stream.jsonl")
                    await self._save_state(
                        state_path,
                        state,
                        completed_worker=directive.worker_id,
                    )
                    return cached, obs_urls, snippets
            except Exception:
                pass

        prompt_file = session_dir / "directives" / f"{directive.worker_id}.prompt.md"
        worker_prompt = prompt_file.read_text(encoding="utf-8")
        system_prompt = (
            "Sen izole bir derin araştırma alt ajanısın (Subagent). "
            "Yalnızca kendi alt konuna odaklan, birincil ve güvenilir kaynaklardan veri topla. "
            "Her bulguda kaynaktan birebir değiştirilmemiş `exact_quote` ve tam `url` ver. "
            "ASLA URL veya alıntı uydurma. Yalnızca SubagentOutput JSON şemasında yanıt ver."
        )
        schema = SubagentOutput.model_json_schema()

        backends: list[tuple[str, str, Path]] = [
            (
                self.cfg.research.worker_backend,
                self.cfg.research.worker_model,
                worker_dir,
            )
        ]
        fb_backend = self.cfg.research.worker_fallback_backend
        fb_model = self.cfg.research.worker_fallback_model
        if fb_backend:
            backends.append((fb_backend, fb_model, worker_dir / "fallback"))

        async with sem:
            last_error = ""
            for idx, (backend, model, run_dir) in enumerate(backends):
                run_dir.mkdir(parents=True, exist_ok=True)
                adapter = self.adapter_factory(backend)
                req = WorkerRequest(
                    worker_id=directive.worker_id,
                    role="subagent",
                    prompt=worker_prompt,
                    system_prompt=system_prompt,
                    work_dir=run_dir,
                    json_schema=schema,
                    model=model,
                    reasoning_effort=self.cfg.research.worker_reasoning_effort,
                    allow_web=True,
                    timeout_secs=self.cfg.research.worker_timeout_secs,
                    max_budget_usd=self.cfg.research.max_budget_usd,
                    unit_name=f"seslinot-w-{job.job_id}-{directive.worker_id}-{idx}",
                )
                res = await self.runner.run(adapter, req)
                obs_urls = list(res.observed_urls)
                snippets = (
                    extract_snippets_from_stream(res.raw_stream_path)
                    if res.raw_stream_path is not None
                    else {}
                )

                if res.ok and res.parsed_json:
                    try:
                        parsed_out = SubagentOutput.model_validate(res.parsed_json)
                        parsed_out.worker_id = directive.worker_id
                        if not parsed_out.subtopic:
                            parsed_out.subtopic = directive.subtopic
                        if parsed_out.status != "failed":
                            findings_path.parent.mkdir(parents=True, exist_ok=True)
                            findings_path.write_text(
                                parsed_out.model_dump_json(indent=2),
                                encoding="utf-8",
                            )
                            _write_json_atomic(observed_path, obs_urls)
                            await self._save_state(
                                state_path,
                                state,
                                completed_worker=directive.worker_id,
                            )
                            return parsed_out, obs_urls, snippets
                        last_error = "; ".join(parsed_out.gaps_or_blockers) or "Worker status=failed"
                    except Exception as exc:
                        last_error = f"SubagentOutput doğrulama hatası: {exc}"
                else:
                    last_error = res.error or f"{backend} worker başarısız oldu"

            failed_out = SubagentOutput(
                worker_id=directive.worker_id,
                subtopic=directive.subtopic,
                status="failed",
                gaps_or_blockers=[last_error or "Alt ajan tamamlanamadı"],
                findings=[],
            )
            findings_path.parent.mkdir(parents=True, exist_ok=True)
            findings_path.write_text(failed_out.model_dump_json(indent=2), encoding="utf-8")
            _write_json_atomic(observed_path, [])
            await self._save_state(state_path, state, failed_worker=directive.worker_id)
            return failed_out, [], {}

    # --- Phase 4a: Synthesis & Repair ---

    def _build_synth_prompts(
        self,
        plan: ResearchPlan,
        quote_checks: list[QuoteVerification],
        registry: dict[str, SourceRecord],
        subagent_outputs: list[SubagentOutput],
    ) -> tuple[str, str]:
        system_prompt = (
            "Sen Türkçe derin araştırma raporları yazan uzman bir sentezleyicisin.\n"
            "KESİN KURALLAR:\n"
            "1. Raporun tamamını akıcı, yapılandırılmış, eyleme geçirilebilir Türkçe Markdown olarak yaz.\n"
            "2. YALNIZCA sana verilen doğrulanmış bulguları ve kilitli kaynak kimliklerini "
            "([^S01], [^V01] vb.) kullan.\n"
            "3. Gövde metninde ASLA ham https:// veya http:// URL yazma.\n"
            "4. ASLA `## Kaynaklar` bölümü veya dipnot tanımı (`[^S01]: ...`) yazma; "
            "kaynakça sistem tarafından deterministik olarak eklenecektir.\n"
            "5. Doğrulanmış bulguların ötesinde iddia veya sayı uydurma; eksikleri 'Açık Sorular ve Boşluklar' "
            "bölümünde açıkça belirt.\n"
        )

        verified_entries: list[str] = []
        valid_statuses = {"exact_match", "near_match", "snippet_only"}
        for qc in quote_checks:
            if qc.quote_status not in valid_statuses:
                continue
            rec = registry.get(qc.source_id)
            if rec is None or rec.fetch_status in ("dead_url", "vault_missing"):
                continue
            verified_entries.append(
                f"- Kaynak: [^{qc.source_id}] | Başlık: {rec.title or rec.source_id} | "
                f"Tarih: {rec.published_at or rec.retrieved_at[:10]} | "
                f"Doğrulama: {qc.quote_status}\n"
                f"  İddia (TR): {qc.claim_tr}\n"
                f"  Doğrulanmış Alıntı: \"{qc.exact_quote}\""
            )

        vault_entries: list[str] = []
        for sid, rec in registry.items():
            if rec.source_type == "vault" and rec.fetch_status == "vault_ok":
                vault_entries.append(f"- [^{sid}] {rec.title} (Hafıza Kasası)")

        gaps: list[str] = []
        for out in subagent_outputs:
            for g in out.gaps_or_blockers:
                if g.strip():
                    gaps.append(f"- ({out.worker_id}) {g.strip()}")

        user_prompt = (
            f"# Araştırma Başlığı: {plan.title_tr}\n\n"
            f"## Özet Çerçeve\n{plan.brief_summary}\n\n"
            f"## Yanıtlanacak Sorular\n"
            + "\n".join(f"- {q}" for q in plan.key_questions)
            + "\n\n## Kullanılabilir Kasa Kaynakları\n"
            + ("\n".join(vault_entries) if vault_entries else "- (Yok)")
            + "\n\n## Doğrulanmış Bulgular (YALNIZCA bunlara atıf yap)\n"
            + (
                "\n".join(verified_entries)
                if verified_entries
                else "- (Doğrulanmış web bulgusu bulunamadı; mevcut boşlukları dürüstçe raporla.)"
            )
            + "\n\n## Alt Ajan Boşlukları / Engeller\n"
            + ("\n".join(gaps) if gaps else "- (Yok)")
            + "\n\nLütfen Yönetici Özeti, Detaylı Analiz, Pratik Öneriler ve Açık Sorular başlıklarıyla "
            "Türkçe raporu yaz (ham URL veya `## Kaynaklar` ekleme)."
        )
        return system_prompt, user_prompt

    async def _run_synthesizer(
        self,
        job: ResearchJob,
        plan: ResearchPlan,
        quote_checks: list[QuoteVerification],
        registry: dict[str, SourceRecord],
        subagent_outputs: list[SubagentOutput],
        session_dir: Path,
    ) -> tuple[str, str]:
        synth_dir = session_dir / "synth"
        system_prompt, user_prompt = self._build_synth_prompts(
            plan, quote_checks, registry, subagent_outputs
        )

        candidates: list[tuple[str, str]] = [
            (self.cfg.research.synth_backend, self.cfg.research.synth_model)
        ]
        if self.cfg.research.worker_backend != self.cfg.research.synth_backend:
            candidates.append(
                (self.cfg.research.worker_backend, self.cfg.research.worker_model)
            )

        last_err = ""
        for idx, (backend, model) in enumerate(candidates):
            run_dir = synth_dir if idx == 0 else synth_dir / f"fallback_{backend}"
            adapter = self.adapter_factory(backend)
            req = WorkerRequest(
                worker_id="synthesizer",
                role="synthesizer",
                prompt=user_prompt,
                system_prompt=system_prompt,
                work_dir=run_dir,
                json_schema=None,
                model=model,
                allow_web=False,
                timeout_secs=self.cfg.research.worker_timeout_secs,
                max_budget_usd=self.cfg.research.max_budget_usd,
                unit_name=f"seslinot-synth-{job.job_id}-{idx}",
            )
            res = await self.runner.run(adapter, req)
            if res.ok and res.output_text.strip():
                draft = res.output_text.strip()
                synth_dir.mkdir(parents=True, exist_ok=True)
                (synth_dir / "draft.md").write_text(draft, encoding="utf-8")
                return draft, backend
            last_err = res.error or f"{backend} sentezleyici boş yanıt döndürdü"

        raise RuntimeError(f"Faz 4a (Sentez) başarısız: {last_err}")

    # --- Full Orchestration ---

    async def run(
        self,
        job: ResearchJob,
        *,
        vault_context: VaultContextBundle | None = None,
        session_dir: Path | None = None,
        triggered_by_rel_path: str = "",
        previous_version_rel_path: str = "",
    ) -> ResearchReportInput:
        """4+1 aşamalı derin araştırma akışını yürütür ve VaultWriter'a hazır ResearchReportInput döndürür."""
        started_mono = time.monotonic()

        # Phase 0: Session dizini ve girdi kalıcılığı
        sess_dir = self._resolve_session_dir(job, session_dir)
        input_dir = sess_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        state_path = sess_dir / "state.json"
        state = self._load_state(state_path, job)

        transcript_path = input_dir / "transcript.md"
        if not transcript_path.is_file():
            transcript_path.write_text(job.raw_prompt, encoding="utf-8")
        vault_ctx_path = input_dir / "vault_context.json"
        if vault_context is not None:
            bundle = vault_context
            if not vault_ctx_path.is_file():
                vault_ctx_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        elif vault_ctx_path.is_file():
            bundle = VaultContextBundle.model_validate_json(
                vault_ctx_path.read_text(encoding="utf-8")
            )
        else:
            bundle = VaultContextBundle(query=job.topic)
            vault_ctx_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        await self._emit_progress(ResearchPhase.VAULT_SCAN, "Girdi ve kasa bağlamı hazırlandı")
        await self._save_state(
            state_path,
            state,
            phase=ResearchPhase.VAULT_SCAN,
            completed_phase=ResearchPhase.VAULT_SCAN.value,
        )

        # Phase 1: Lead Agent yön tayini
        plan = await self._run_phase_direction(job, bundle, sess_dir, state, state_path)

        # Phase 2: Paralel izole Subagent'lar
        await self._emit_progress(
            ResearchPhase.GATHERING,
            f"{len(plan.directives)} alt ajan paralel araştırma yapıyor",
        )
        sem = asyncio.Semaphore(max(1, self.cfg.research.max_subagents))
        worker_results = await asyncio.gather(
            *(
                self._run_single_subagent(job, d, sess_dir, sem, state, state_path)
                for d in plan.directives
            )
        )

        subagent_outputs: list[SubagentOutput] = []
        observed_urls_by_worker: dict[str, list[str]] = {}
        merged_snippets: dict[str, list[str]] = {}

        for out, obs_urls, snippets in worker_results:
            subagent_outputs.append(out)
            observed_urls_by_worker[out.worker_id] = obs_urls
            for url_key, snips in snippets.items():
                merged_snippets.setdefault(url_key, []).extend(snips)

        await self._save_state(
            state_path,
            state,
            phase=ResearchPhase.GATHERING,
            completed_phase=ResearchPhase.GATHERING.value,
        )

        # Phase 3: 0-token deterministik SourceRegistry + Span.verify
        await self._emit_progress(ResearchPhase.VERIFYING)
        registry = SourceRegistry(
            sess_dir,
            self.cfg.vault.path,
            self.cfg,
            fetcher=self.fetcher,
        )
        registry.ingest_vault_context(bundle)
        finding_source_map = registry.ingest_subagent_outputs(
            subagent_outputs,
            observed_urls_by_worker,
        )
        await registry.fetch_all()

        quote_checks = verify_all_findings(
            subagent_outputs,
            registry,
            merged_snippets,
            finding_source_map=finding_source_map,
            vault_path=self.cfg.vault.path,
        )
        await self._save_state(
            state_path,
            state,
            phase=ResearchPhase.VERIFYING,
            completed_phase=ResearchPhase.VERIFYING.value,
        )

        # Phase 4a: Türkçe sentez
        await self._emit_progress(ResearchPhase.SYNTHESIZING)
        draft_md, synth_backend_used = await self._run_synthesizer(
            job,
            plan,
            quote_checks,
            registry.records,
            subagent_outputs,
            sess_dir,
        )
        await self._save_state(
            state_path,
            state,
            phase=ResearchPhase.SYNTHESIZING,
            completed_phase=ResearchPhase.SYNTHESIZING.value,
        )

        # Phase 4b: Deterministik atıf kapısı + Çapraz şirket denetçi + Onarım + Deterministik kaynakça
        await self._emit_progress(ResearchPhase.AUDITING)
        valid_ids = sorted(
            sid
            for sid, rec in registry.records.items()
            if rec.fetch_status not in ("dead_url", "vault_missing")
        )
        quotes_lookup: dict[str, list[str]] = {}
        for qc in quote_checks:
            if qc.quote_status in ("exact_match", "near_match", "snippet_only") and qc.source_id:
                quotes_lookup.setdefault(qc.source_id, []).append(qc.exact_quote)

        async def _targeted_repair(current_draft: str, issues: dict[str, Any]) -> str:
            round_num = int(issues.get("round", 1))
            repair_dir = sess_dir / "synth" / f"repair_{round_num}"
            repair_adapter = self.adapter_factory(synth_backend_used)
            repair_sys = (
                "Sana verilen Türkçe araştırma taslağındaki YALNIZCA işaretlenmiş atıf ve doğruluk "
                "hatalarını düzelt. Diğer doğrulanmış cümleleri koru. Gövdeye ham URL veya "
                "`## Kaynaklar` ekleme. Yalnızca geçerli kaynak kimliklerini kullan."
            )
            repair_prompt = (
                f"Geçerli Kaynak Kimlikleri: {', '.join(valid_ids) or '(yok)'}\n\n"
                f"Doğrulanmış Alıntılar:\n{json.dumps(quotes_lookup, ensure_ascii=False, indent=2)}\n\n"
                f"Tespit Edilen Sorunlar:\n{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
                f"Mevcut Taslak:\n{current_draft}\n\n"
                "Lütfen sorunları giderip düzeltilmiş Türkçe Markdown gövdesini döndür."
            )
            repair_model = (
                self.cfg.research.synth_model
                if synth_backend_used == self.cfg.research.synth_backend
                else self.cfg.research.worker_model
            )
            req = WorkerRequest(
                worker_id=f"synthesizer_repair_{round_num}",
                role="synthesizer",
                prompt=repair_prompt,
                system_prompt=repair_sys,
                work_dir=repair_dir,
                json_schema=None,
                model=repair_model,
                allow_web=False,
                timeout_secs=min(240, self.cfg.research.worker_timeout_secs),
                max_budget_usd=self.cfg.research.max_budget_usd,
                unit_name=f"seslinot-repair-{job.job_id}-{round_num}",
            )
            res = await self.runner.run(repair_adapter, req)
            if res.ok and res.output_text.strip():
                repaired_text = res.output_text.strip()
                (repair_dir / "repaired_draft.md").write_text(repaired_text, encoding="utf-8")
                return repaired_text
            return current_draft

        auditor = CitationAuditor(
            self.cfg,
            adapter_factory=self.adapter_factory,
            runner=self.runner,
        )
        audited_draft, verification = await auditor.audit_and_repair(
            draft_md,
            quote_checks,
            registry.records,
            sess_dir / "audit",
            synth_backend=synth_backend_used,
            repair_fn=_targeted_repair,
            job_id=job.job_id,
        )

        final_md, cited_sources = render_deterministic_report(audited_draft, registry.records)
        (sess_dir / "verification.json").write_text(
            verification.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (sess_dir / "final.md").write_text(final_md, encoding="utf-8")

        await self._save_state(
            state_path,
            state,
            phase=ResearchPhase.AUDITING,
            completed_phase=ResearchPhase.AUDITING.value,
        )

        # Güven düzeyi hesapla
        strong_quotes = verification.quote_exact_match + verification.quote_near_match
        if (
            verification.verified_sources >= 2
            and strong_quotes >= 2
            and verification.audit_status in ("passed", "repaired")
        ):
            confidence = "yüksek"
        elif verification.verified_sources >= 1 and verification.audit_status != "failed_closed":
            confidence = "orta"
        else:
            confidence = "düşük"

        vault_links = [
            rec.vault_path
            for rec in registry.ordered_records()
            if rec.source_type == "vault" and rec.fetch_status == "vault_ok" and rec.vault_path
        ]

        duration_secs = max(0, int(round(time.monotonic() - started_mono)))
        return ResearchReportInput(
            report_id=job.job_id,
            title=plan.title_tr or job.topic,
            topic_slug=plan.topic_slug or _slugify(job.topic),
            scope=plan.scope or bundle.scope or "user",
            topic=job.topic,
            concepts=plan.concepts or list(bundle.matched_concepts),
            triggered_by_rel_path=triggered_by_rel_path,
            previous_version_rel_path=previous_version_rel_path,
            freshness_months=plan.freshness_months or self.cfg.research.default_freshness_months,
            body_markdown=final_md,
            sources=cited_sources,
            verification=verification,
            agent_count=len(plan.directives),
            duration_secs=duration_secs,
            confidence=confidence,  # type: ignore[arg-type]
            vault_context_links=vault_links,
        )

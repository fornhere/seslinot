"""OpenCode CLI worker adaptörü (ephemeral opencode.json izin engelleme ile)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from seslinot.research.adapters.base import (
    WorkerRequest,
    WorkerResult,
    extract_json_from_text,
    extract_urls_from_stream,
)


class OpenCodeAdapter:
    """Yerel `opencode run` CLI'yi bash/edit/write izinleri kapalı olarak çalıştırır."""

    backend_name: str = "opencode"

    def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
        req.work_dir.mkdir(parents=True, exist_ok=True)

        permission_cfg: dict[str, Any] = {
            "bash": {"*": "deny"},
            "edit": "deny",
            "write": "deny",
        }
        if not req.allow_web:
            permission_cfg["webfetch"] = "deny"
            permission_cfg["websearch"] = "deny"

        ephemeral_cfg = {"permission": permission_cfg}
        (req.work_dir / "opencode.json").write_text(
            json.dumps(ephemeral_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        prompt_parts: list[str] = []
        if req.system_prompt:
            prompt_parts.append(f"<system>\n{req.system_prompt.strip()}\n</system>")
        prompt_parts.append(req.prompt)
        if req.json_schema is not None:
            schema_json = json.dumps(req.json_schema, ensure_ascii=False)
            prompt_parts.append(
                f"Yanıtını YALNIZCA aşağıdaki JSON şemasına uyan geçerli bir JSON nesnesi olarak ver:\n{schema_json}"
            )
        full_prompt = "\n\n".join(prompt_parts)

        cmd: list[str] = [
            "opencode",
            "run",
            "--format",
            "json",
        ]
        if req.model:
            cmd.extend(["--model", req.model])
        cmd.append(full_prompt)

        return cmd, None, None

    def parse_output(
        self,
        req: WorkerRequest,
        stdout_path: Path,
        output_file_path: Path | None,
    ) -> WorkerResult:
        del output_file_path
        observed_urls = extract_urls_from_stream(stdout_path)

        if not stdout_path.is_file():
            return WorkerResult(
                ok=False,
                output_text="",
                parsed_json=None,
                raw_stream_path=stdout_path,
                observed_urls=observed_urls,
                backend=self.backend_name,
                error="OpenCode çıktı akışı bulunamadı",
            )

        raw_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        chunks: list[str] = []
        error_msg = ""
        parsed_any_jsonl = False

        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_any_jsonl = True

            ev_type = str(event.get("type", "")).lower()
            if ev_type == "error":
                error_msg = str(event.get("error") or event.get("message") or "OpenCode error")
            elif ev_type == "text":
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif isinstance(event.get("text"), str):
                    chunks.append(event["text"])
            elif ev_type == "result" and isinstance(event.get("result"), str):
                chunks.append(event["result"])

        output_text = "\n".join(chunks).strip()
        if not output_text and not parsed_any_jsonl:
            output_text = raw_text.strip()

        parsed_json = extract_json_from_text(output_text)
        ok = (not error_msg) and bool(output_text)
        if req.json_schema is not None and parsed_json is None:
            ok = False
            if not error_msg:
                error_msg = "OpenCode çıktısından geçerli JSON üretilemedi"

        return WorkerResult(
            ok=ok,
            output_text=output_text,
            parsed_json=parsed_json,
            raw_stream_path=stdout_path,
            observed_urls=observed_urls,
            backend=self.backend_name,
            error=error_msg,
        )

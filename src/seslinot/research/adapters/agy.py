"""Agy CLI worker adaptörü (sandbox + plan modu + stream-json)."""

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


class AgyAdapter:
    """Yerel `agy` CLI'yi sandbox/plan modunda ve zaman aşımı korumalı çalıştırır."""

    backend_name: str = "agy"

    def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
        req.work_dir.mkdir(parents=True, exist_ok=True)
        cmd: list[str] = [
            "agy",
            "--sandbox",
            "--mode",
            "plan",
            "--disable-slash-commands",
            "--print-timeout",
            f"{req.timeout_secs}s",
            "--output-format",
            "stream-json",
        ]

        if req.model:
            cmd.extend(["--model", req.model])

        if req.json_schema is not None:
            schema_file = req.work_dir / "schema.json"
            schema_file.write_text(
                json.dumps(req.json_schema, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            cmd.extend(["--json-schema", str(schema_file)])

        full_prompt = req.prompt
        if req.system_prompt:
            full_prompt = f"<system>\n{req.system_prompt.strip()}\n</system>\n\n{req.prompt}"

        cmd.append(f"--print={full_prompt}")
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
                error="Agy çıktı akışı bulunamadı",
            )

        raw_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        result_text = ""
        structured_output: dict[str, Any] | None = None
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
            if ev_type == "error" or event.get("is_error") or event.get("status") == "ERROR":
                error_msg = str(event.get("error") or event.get("message") or event.get("result") or "Agy error")
            elif isinstance(event.get("response"), str) and event["response"].strip():
                result_text = event["response"]
            elif ev_type == "result":
                if isinstance(event.get("structured_output"), dict):
                    structured_output = event["structured_output"]
                if isinstance(event.get("result"), str) and event["result"].strip():
                    result_text = event["result"]
            elif ev_type in {"assistant", "message", "text"}:
                if isinstance(event.get("text"), str) and event["text"]:
                    chunks.append(event["text"])
                msg = event.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            txt = block.get("text")
                            if isinstance(txt, str) and txt:
                                chunks.append(txt)
                elif isinstance(event.get("content"), str) and event["content"]:
                    chunks.append(event["content"])

        if not result_text and chunks:
            result_text = "\n".join(chunks)
        if not result_text and structured_output is not None:
            result_text = json.dumps(structured_output, ensure_ascii=False)
        if not parsed_any_jsonl and raw_text.strip():
            result_text = raw_text.strip()

        parsed_json = structured_output or extract_json_from_text(result_text)
        ok = (not error_msg) and bool(result_text or parsed_json is not None)
        if req.json_schema is not None and parsed_json is None:
            ok = False
            if not error_msg:
                error_msg = "Agy çıktısından geçerli JSON üretilemedi"

        return WorkerResult(
            ok=ok,
            output_text=result_text,
            parsed_json=parsed_json,
            raw_stream_path=stdout_path,
            observed_urls=observed_urls,
            backend=self.backend_name,
            error=error_msg,
        )

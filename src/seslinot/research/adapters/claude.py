"""Claude Code CLI worker adaptörü (OAuth güvenli, --bare ASLA kullanılmaz)."""

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


class ClaudeAdapter:
    """Yerel `claude` CLI'yi kısıtlı, etkileşimsiz ve stream-json modunda çalıştırır."""

    backend_name: str = "claude"

    def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
        req.work_dir.mkdir(parents=True, exist_ok=True)
        cmd: list[str] = [
            "claude",
            "--print",
            "--restricted",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-budget-usd",
            f"{req.max_budget_usd:.2f}",
        ]

        if req.model:
            cmd.extend(["--model", req.model])

        if req.system_prompt:
            cmd.extend(["--system-prompt", req.system_prompt])

        if req.allow_web:
            cmd.extend(
                [
                    "--tools",
                    "Read,Write,Grep,Glob,WebSearch,WebFetch",
                    "--allowedTools",
                    "Read,Write,Grep,Glob,WebSearch,WebFetch",
                    "--disallowedTools",
                    "Bash,Edit,NotebookEdit",
                ]
            )
        else:
            cmd.extend(
                [
                    "--tools",
                    "",
                    "--disallowedTools",
                    "Bash,Edit,NotebookEdit,Write",
                ]
            )

        if req.json_schema is not None:
            schema_str = json.dumps(req.json_schema, ensure_ascii=False, separators=(",", ":"))
            (req.work_dir / "schema.json").write_text(
                json.dumps(req.json_schema, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            cmd.extend(["--json-schema", schema_str])

        stdin_bytes = req.prompt.encode("utf-8")
        return cmd, stdin_bytes, None

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
                error="Claude çıktı akışı bulunamadı",
            )

        raw_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        result_text = ""
        structured_output: dict[str, Any] | None = None
        assistant_chunks: list[str] = []
        is_error = False
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

            ev_type = event.get("type")
            if ev_type == "result":
                if event.get("is_error"):
                    is_error = True
                    error_msg = str(event.get("result") or event.get("error") or "Claude result error")
                if isinstance(event.get("structured_output"), dict):
                    structured_output = event["structured_output"]
                if isinstance(event.get("result"), str) and event["result"].strip():
                    result_text = event["result"]
            elif ev_type == "assistant":
                msg = event.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            txt = block.get("text")
                            if isinstance(txt, str) and txt:
                                assistant_chunks.append(txt)

        if not result_text and assistant_chunks:
            result_text = "\n".join(assistant_chunks)
        if not result_text and structured_output is not None:
            result_text = json.dumps(structured_output, ensure_ascii=False)
        if not parsed_any_jsonl and raw_text.strip():
            result_text = raw_text.strip()

        parsed_json = structured_output or extract_json_from_text(result_text)
        ok = (not is_error) and bool(result_text or parsed_json is not None)
        if req.json_schema is not None and parsed_json is None:
            ok = False
            if not error_msg:
                error_msg = "Claude çıktısından geçerli JSON üretilemedi"

        return WorkerResult(
            ok=ok,
            output_text=result_text,
            parsed_json=parsed_json,
            raw_stream_path=stdout_path,
            observed_urls=observed_urls,
            backend=self.backend_name,
            error=error_msg,
        )

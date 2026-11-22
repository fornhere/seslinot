"""OpenAI Codex CLI worker adaptörü (kabuk kapalı, salt-okunur sandbox)."""

from __future__ import annotations

import json
from pathlib import Path

from seslinot.research.adapters.base import (
    WorkerRequest,
    WorkerResult,
    ensure_strict_json_schema,
    extract_json_from_text,
    extract_urls_from_stream,
)


class CodexAdapter:
    """Yerel `codex exec` CLI'yi shell kapalı ve read-only sandbox modunda çalıştırır."""

    backend_name: str = "codex"

    def build_command(self, req: WorkerRequest) -> tuple[list[str], bytes | None, Path | None]:
        req.work_dir.mkdir(parents=True, exist_ok=True)
        out_file = req.work_dir / "codex_last_message.txt"

        cmd: list[str] = [
            "codex",
            "exec",
            "--skip-git-repo-check",
        ]

        if req.model:
            cmd.extend(["-m", req.model])

        if req.reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{req.reasoning_effort}"'])

        cmd.extend(
            [
                "--disable",
                "shell_tool",
                "-s",
                "read-only",
                "-C",
                str(req.work_dir),
                "--color",
                "never",
                "--json",
                "-o",
                str(out_file),
            ]
        )

        if req.allow_web:
            cmd.extend(["-c", 'web_search="live"'])

        if req.json_schema is not None:
            strict_schema = ensure_strict_json_schema(req.json_schema)
            schema_file = req.work_dir / "output_schema.json"
            schema_file.write_text(
                json.dumps(strict_schema, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            cmd.extend(["--output-schema", str(schema_file)])

        cmd.append("-")

        full_prompt = req.prompt
        if req.system_prompt:
            full_prompt = f"<system>\n{req.system_prompt.strip()}\n</system>\n\n{req.prompt}"

        return cmd, full_prompt.encode("utf-8"), out_file

    def parse_output(
        self,
        req: WorkerRequest,
        stdout_path: Path,
        output_file_path: Path | None,
    ) -> WorkerResult:
        observed_urls = extract_urls_from_stream(stdout_path)
        output_text = ""

        if output_file_path is not None and output_file_path.is_file():
            output_text = output_file_path.read_text(encoding="utf-8", errors="replace").strip()

        error_msg = ""
        if not output_text and stdout_path.is_file():
            agent_messages: list[str] = []
            raw_text = stdout_path.read_text(encoding="utf-8", errors="replace")
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
                if ev_type == "error":
                    error_msg = str(event.get("message") or event.get("error") or "Codex error")
                elif ev_type == "item.completed":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        txt = item.get("text")
                        if isinstance(txt, str) and txt.strip():
                            agent_messages.append(txt.strip())

            if agent_messages:
                output_text = agent_messages[-1]
            elif not parsed_any_jsonl and raw_text.strip():
                output_text = raw_text.strip()

        parsed_json = extract_json_from_text(output_text)
        ok = bool(output_text) and not error_msg
        if req.json_schema is not None and parsed_json is None:
            ok = False
            if not error_msg:
                error_msg = "Codex çıktısından geçerli JSON üretilemedi"

        return WorkerResult(
            ok=ok,
            output_text=output_text,
            parsed_json=parsed_json,
            raw_stream_path=stdout_path,
            observed_urls=observed_urls,
            backend=self.backend_name,
            error=error_msg,
        )

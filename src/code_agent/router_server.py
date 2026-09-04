from __future__ import annotations

import argparse
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ArchRouterRuntime:
    """Lazy optional runtime for katanemo/Arch-Router-1.5B."""

    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                'Semantic router dependencies are missing. Run: pip install -e ".[semantic-router]"'
            ) from exc

        self._torch = torch
        self._lock = threading.Lock()
        cache = str(cache_dir) if cache_dir else None
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, cache_dir=cache)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            cache_dir=cache,
            torch_dtype="auto",
        )
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)
        self.model.eval()

    def complete(self, prompt: str, max_tokens: int = 64) -> tuple[str, int, int]:
        encoded = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )
        model_inputs = {key: value.to(self.device) for key, value in encoded.items()}
        input_ids = model_inputs["input_ids"]
        with self._lock, self._torch.inference_mode():
            generated = self.model.generate(
                **model_inputs,
                max_new_tokens=max(1, min(max_tokens, 128)),
                do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        output_ids = generated[0][input_ids.shape[-1]:]
        content = self.tokenizer.decode(output_ids, skip_special_tokens=True).strip()
        return content, int(input_ids.shape[-1]), int(output_ids.shape[-1])


def _handler(runtime: ArchRouterRuntime, model_name: str) -> type[BaseHTTPRequestHandler]:
    class RouterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._json(200, {"ok": True, "model": model_name})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in {"/v1/chat/completions", "/chat/completions"}:
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                prompt = self._last_user_text(body.get("messages"))
                if not prompt:
                    raise ValueError("A non-empty user message is required.")
                content, prompt_tokens, completion_tokens = runtime.complete(
                    prompt,
                    int(body.get("max_tokens") or 64),
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:
                traceback.print_exc()
                self._json(500, {"error": f"{type(exc).__name__}: {exc!r}"})
                return

            self._json(
                200,
                {
                    "id": "arch-router-local",
                    "object": "chat.completion",
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                },
            )

        @staticmethod
        def _last_user_text(messages: Any) -> str:
            if not isinstance(messages, list):
                return ""
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    return str(message.get("content") or "")
            return ""

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return RouterHandler


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve Arch-Router through an OpenAI-compatible API."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--model", default="katanemo/Arch-Router-1.5B")
    parser.add_argument("--cache-dir", type=Path, default=Path(".code_agent/model-cache"))
    args = parser.parse_args()

    cache_dir = args.cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"Loading {args.model} into {cache_dir} ...", flush=True)
    runtime = ArchRouterRuntime(args.model, cache_dir)
    server = ThreadingHTTPServer((args.host, args.port), _handler(runtime, args.model))
    print(f"Semantic router listening on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

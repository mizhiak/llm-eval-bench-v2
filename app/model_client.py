"""统一的大模型调用客户端，支持多种 API 格式。"""
import time
import json
import httpx


class ModelClient:
    """支持 OpenAI 兼容 / vLLM / Ollama 等多种格式的统一客户端。"""

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 api_format: str = "openai", timeout: float = 120.0,
                 extra_headers: dict | None = None, disable_thinking: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.api_format = api_format  # openai | ollama | vllm | raw_completions | anthropic
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.disable_thinking = disable_thinking

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_format == "anthropic":
            # Anthropic uses x-api-key header; skip Authorization to avoid confusing the server
            pass
        elif self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.api_key:
            h["x-api-key"] = self.api_key
        h["anthropic-version"] = "2023-06-01" if self.api_format == "anthropic" else h.get("anthropic-version", "")
        if not h.get("anthropic-version"):
            del h["anthropic-version"]
        h.update(self.extra_headers)
        return h

    def _endpoint(self) -> str:
        fmt = self.api_format
        if fmt == "ollama":
            # Ollama 原生 chat 接口
            if self.base_url.endswith("/api/chat"):
                return self.base_url
            return f"{self.base_url}/api/chat"
        if fmt == "raw_completions":
            if "/completions" in self.base_url:
                return self.base_url
            return f"{self.base_url}/v1/completions"
        if fmt == "anthropic":
            if "/messages" in self.base_url:
                return self.base_url
            return f"{self.base_url}/v1/messages"
        # openai / vllm 都走 chat/completions
        # 兼容单复数两种拼写：/chat/completion 和 /chat/completions
        if "/chat/completion" in self.base_url:
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def _build_payload(self, prompt: str, max_tokens: int, temperature: float,
                       stream: bool, system: str | None) -> dict:
        fmt = self.api_format
        # 关闭思考：Qwen3 等模型可在 prompt 末尾加 /no_think（模板级开关）
        user_content = prompt
        if self.disable_thinking:
            user_content = prompt + " /no_think"

        if fmt == "ollama":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": user_content})
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": stream,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
            if self.disable_thinking:
                payload["think"] = False  # Ollama 原生关闭思考字段
            return payload
        if fmt == "raw_completions":
            return {
                "model": self.model,
                "prompt": user_content,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": stream,
            }
        # openai / vllm
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if self.disable_thinking:
            # vLLM 部署 Qwen3 的标准关闭思考方式
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if fmt == "anthropic":
            if system:
                payload["system"] = system  # Anthropic: system is top-level field
            # Remove max_tokens if it's 0 (Anthropic API requires it, but u-VLLM handles it)
            if not payload.get("max_tokens"):
                payload["max_tokens"] = 512
        return payload

    def _parse_non_stream(self, data: dict) -> str:
        """返回最终回答文本（兼容 content 为空时回退 reasoning）。"""
        content, reasoning = self._parse_parts(data)
        return content or reasoning or ""

    def _parse_parts(self, data: dict) -> tuple[str, str]:
        """返回 (content, reasoning_content) 二元组。

        强思考模型的 vLLM 部署通常把思考放 reasoning_content、最终答案放 content，
        分开返回让评测优先用干净的 content 解析答案。
        """
        fmt = self.api_format
        try:
            if fmt == "anthropic":
                content_blocks = data.get("content", [])
                text_parts = []
                thinking_parts = []
                for block in content_blocks:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "thinking":
                            thinking_parts.append(block.get("thinking", ""))
                return ("".join(text_parts), "".join(thinking_parts))
            if fmt == "ollama":
                msg = data.get("message") or {}
                return (msg.get("content") or "", msg.get("reasoning_content") or "")
            if fmt == "raw_completions":
                return (data["choices"][0].get("text") or "", "")
            msg = data["choices"][0].get("message") or {}
            return (msg.get("content") or "", msg.get("reasoning_content") or "")
        except (KeyError, IndexError, TypeError):
            return ("", "")

    def chat(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0,
             system: str | None = None) -> dict:
        """非流式调用，返回 {text, latency, prompt_tokens, completion_tokens, ok, error}。"""
        payload = self._build_payload(prompt, max_tokens, temperature, False, system)
        fmt = self.api_format
        t0 = time.perf_counter()
        result = {"text": "", "reasoning": "", "latency": 0.0, "prompt_tokens": 0,
                  "completion_tokens": 0, "ok": False, "error": None}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(self._endpoint(), headers=self._headers(), json=payload)
                r.raise_for_status()
                data = r.json()
            result["latency"] = time.perf_counter() - t0
            content, reasoning = self._parse_parts(data)
            # text 用于解析答案：优先 content，content 空则用 reasoning 兜底
            result["text"] = content or reasoning or ""
            result["reasoning"] = reasoning
            result["has_content"] = bool(content)
            usage = data.get("usage", {}) or {}
            if fmt == "anthropic":
                result["prompt_tokens"] = usage.get("input_tokens", 0)
                result["completion_tokens"] = usage.get("output_tokens", 0)
            else:
                result["prompt_tokens"] = usage.get("prompt_tokens", 0)
                result["completion_tokens"] = usage.get("completion_tokens",
                                                         data.get("eval_count", 0))
            result["ok"] = True
        except httpx.HTTPStatusError as e:
            result["latency"] = time.perf_counter() - t0
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            result["error"] = f"HTTP {e.response.status_code}: {body}"
        except Exception as e:
            result["latency"] = time.perf_counter() - t0
            result["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        return result

    def chat_with_retry(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0,
                        system: str | None = None, max_retries: int = 2,
                        retry_on_empty: bool = False) -> dict:
        """带重试的 chat。失败时重试 max_retries 次（指数退避）。

        retry_on_empty=True 时，输出为空也触发重试（思考型模型偶发空输出）。
        返回结果里附加 attempts 字段记录尝试次数。
        """
        last = None
        for attempt in range(max_retries + 1):
            res = self.chat(prompt, max_tokens=max_tokens,
                           temperature=temperature, system=system)
            last = res
            ok = res["ok"]
            if ok and retry_on_empty and not (res.get("text") or "").strip():
                ok = False  # 空输出视为需要重试
            if ok:
                res["attempts"] = attempt + 1
                return res
            if attempt < max_retries:
                time.sleep(min(2 ** attempt * 0.5, 4))  # 0.5s,1s,2s... 最多4s
        last["attempts"] = max_retries + 1
        return last

    def chat_stream(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0,
                    system: str | None = None):
        """流式调用，逐块 yield 文本片段，并在内部记录 TTFT。"""
        payload = self._build_payload(prompt, max_tokens, temperature, True, system)
        fmt = self.api_format
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", self._endpoint(), headers=self._headers(),
                               json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    if fmt == "ollama":
                        try:
                            obj = json.loads(line)
                            chunk = obj.get("message", {}).get("content", "")
                            if chunk:
                                yield chunk
                            if obj.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
                    else:
                        if fmt == "anthropic":
                            # Anthropic SSE: data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}
                            if line.startswith("data: "):
                                line = line[6:]
                            if not line.strip():
                                continue
                            try:
                                obj = json.loads(line)
                                if obj.get("type") == "content_block_delta":
                                    delta = obj.get("delta", {})
                                    chunk = delta.get("text", "")
                                    if chunk:
                                        yield chunk
                                elif obj.get("type") == "message_stop":
                                    break
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                        else:
                            if line.startswith("data: "):
                                line = line[6:]
                            if line.strip() == "[DONE]":
                                break
                            try:
                                obj = json.loads(line)
                                if fmt == "raw_completions":
                                    chunk = obj["choices"][0].get("text", "")
                                else:
                                    delta = obj["choices"][0].get("delta", {})
                                    chunk = delta.get("content", "")
                                if chunk:
                                    yield chunk
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue

    def chat_collect(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0,
                     system: str | None = None) -> dict:
        """流式调用但组装成完整结果（与 chat 返回结构一致）。

        流式可避免长输出整体超时，对强思考模型更稳。分别收集 content 与
        reasoning_content 两路增量。
        """
        payload = self._build_payload(prompt, max_tokens, temperature, True, system)
        fmt = self.api_format
        t0 = time.perf_counter()
        result = {"text": "", "reasoning": "", "latency": 0.0, "prompt_tokens": 0,
                  "completion_tokens": 0, "ok": False, "error": None, "has_content": False}
        content_parts, reasoning_parts = [], []
        ttft = None
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", self._endpoint(), headers=self._headers(),
                                   json=payload) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        if fmt == "ollama":
                            try:
                                obj = json.loads(line)
                                msg = obj.get("message", {})
                                if msg.get("content"):
                                    if ttft is None:
                                        ttft = time.perf_counter() - t0
                                    content_parts.append(msg["content"])
                                if msg.get("reasoning_content"):
                                    if ttft is None:
                                        ttft = time.perf_counter() - t0
                                    reasoning_parts.append(msg["reasoning_content"])
                                if obj.get("done"):
                                    break
                            except json.JSONDecodeError:
                                continue
                        else:
                            if fmt == "anthropic":
                                # Anthropic SSE stream
                                s = line[6:] if line.startswith("data: ") else line
                                if not s.strip():
                                    continue
                                try:
                                    obj = json.loads(s)
                                    evt = obj.get("type", "")
                                    if evt == "content_block_delta":
                                        delta = obj.get("delta", {})
                                        if delta.get("type") == "text_delta":
                                            txt = delta.get("text", "")
                                            if txt:
                                                if ttft is None:
                                                    ttft = time.perf_counter() - t0
                                                content_parts.append(txt)
                                        elif delta.get("type") == "thinking_delta":
                                            think = delta.get("thinking", "")
                                            if think:
                                                if ttft is None:
                                                    ttft = time.perf_counter() - t0
                                                reasoning_parts.append(think)
                                    elif evt == "message_delta":
                                        usage = obj.get("usage", {})
                                        if usage:
                                            result["prompt_tokens"] = usage.get("input_tokens", 0)
                                            result["completion_tokens"] = usage.get("output_tokens", 0)
                                    elif evt == "message_stop":
                                        break
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    continue
                            else:
                                s = line[6:] if line.startswith("data: ") else line
                                if s.strip() == "[DONE]":
                                    break
                                try:
                                    obj = json.loads(s)
                                    ch = obj["choices"][0]
                                    if fmt == "raw_completions":
                                        if ch.get("text"):
                                            if ttft is None:
                                                ttft = time.perf_counter() - t0
                                            content_parts.append(ch["text"])
                                    else:
                                        delta = ch.get("delta", {})
                                        if delta.get("content"):
                                            if ttft is None:
                                                ttft = time.perf_counter() - t0
                                            content_parts.append(delta["content"])
                                        if delta.get("reasoning_content"):
                                            if ttft is None:
                                                ttft = time.perf_counter() - t0
                                            reasoning_parts.append(delta["reasoning_content"])
                                    if obj.get("usage"):
                                        result["prompt_tokens"] = obj["usage"].get("prompt_tokens", 0)
                                        result["completion_tokens"] = obj["usage"].get("completion_tokens", 0)
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    continue
            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
            result["latency"] = time.perf_counter() - t0
            result["ttft"] = ttft if ttft is not None else result["latency"]
            result["text"] = content or reasoning or ""
            result["reasoning"] = reasoning
            result["has_content"] = bool(content)
            # 流式无 usage 时，用字符估算输出 token
            if not result["completion_tokens"]:
                result["completion_tokens"] = max(1, int(len(result["text"]) * 0.6))
            result["ok"] = True
        except httpx.HTTPStatusError as e:
            result["latency"] = time.perf_counter() - t0
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            result["error"] = f"HTTP {e.response.status_code}: {body}"
        except Exception as e:
            result["latency"] = time.perf_counter() - t0
            result["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        return result

    def chat_auto(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0,
                  system: str | None = None, stream: bool = False,
                  max_retries: int = 2, retry_on_empty: bool = True) -> dict:
        """按 stream 选择流式/非流式，带重试。供精度评测统一调用。"""
        last = None
        for attempt in range(max_retries + 1):
            if stream:
                res = self.chat_collect(prompt, max_tokens, temperature, system)
            else:
                res = self.chat(prompt, max_tokens, temperature, system)
            last = res
            ok = res["ok"]
            if ok and retry_on_empty and not (res.get("text") or "").strip():
                ok = False
            if ok:
                res["attempts"] = attempt + 1
                return res
            if attempt < max_retries:
                time.sleep(min(2 ** attempt * 0.5, 4))
        last["attempts"] = max_retries + 1
        return last

    def test_connection(self) -> dict:
        """测试连通性：max_tokens=1 + 关闭思考，最小化延迟。"""
        saved_timeout = self.timeout
        saved_thinking = self.disable_thinking
        self.timeout = min(self.timeout, 120)
        self.disable_thinking = True  # 跳过 reasoning，加速首 token
        try:
            res = self.chat("hi", max_tokens=1, temperature=0.0)
        finally:
            self.timeout = saved_timeout
            self.disable_thinking = saved_thinking
        return {"ok": res["ok"], "error": res.get("error"),
                "latency": round(res.get("latency", 0), 3)}

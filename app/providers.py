from abc import ABC, abstractmethod
import logging
import re
import time
import httpcore
import requests
from google import genai
from google.genai import types
from ollama import Client, RequestError as OllamaRequestError

from common.config import Config, get_config
from app.context import get_file_context
from app.utils import get_error_files_from_dbt_log, get_instruction

TRANSIENT_PROVIDER_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    httpcore.NetworkError,
    httpcore.TimeoutException,
    OllamaRequestError,
)

SOURCE_PATH_RE = re.compile(r"(?m)^SOURCE OF\s+([^:\n]+):")
SOLUTION_BLOCK_RE = re.compile(
    r"<solution>(.*?)</solution>\s*<file>(.*?)</file>(?:\s*<summary>(.*?)</summary>)?",
    re.DOTALL,
)


def retry_request(call, attempts: int = 3):
    """Retry provider calls after transient network errors."""
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except TRANSIENT_PROVIDER_ERRORS as exc:
            if attempt == attempts:
                raise
            logging.warning(
                "Provider request failed with transient network error (%s/%s): %s. Retrying in %ss",
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            delay *= 2


def normalize_response(text: str) -> str:
    """Strip model reasoning wrappers from response text."""
    if "<think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def source_paths(file_context: str) -> list[str]:
    """Extract source paths from file context."""
    return list(dict.fromkeys(path.strip() for path in SOURCE_PATH_RE.findall(file_context)))


def build_solution_prompt(context: str | list[str] | None, file_context: str) -> str:
    """Build prompt payload for solution generation."""
    if isinstance(context, list):
        context = "\n".join(map(str, context))
    return (
        f"<DBT_ERROR>\n{context or ''}\n</DBT_ERROR>\n\n"
        f"<SOURCE_CONTEXT>\n{file_context}\n</SOURCE_CONTEXT>"
    )


def build_review_prompt(review_context: str) -> str:
    """Build prompt payload for change review."""
    return f"<REVIEW_CONTEXT>\n{review_context or ''}\n</REVIEW_CONTEXT>"


def no_fix_solution(file_context: str) -> str:
    """Build fallback solution block without code changes."""
    paths = source_paths(file_context)
    file = paths[0] if paths else "NO_FILE"
    return f"<solution>NO_FIX</solution>\n<file>\n{file}\n</file>"


def repair_prompt(file_context: str, bad_response: str) -> str:
    """Build prompt for repairing malformed solution output."""
    paths = source_paths(file_context)
    file = paths[0] if paths else "NO_FILE"
    return f"""Your previous response did not match the required tags.
Return only one valid block for this exact file path: {file}

If the previous response contains full corrected file content, put that content in <solution>.
For a real fix, include <summary> after <file> with Russian Изменено, Ошибка, and Причина lines.
Otherwise return NO_FIX.

Valid fallback:
<solution>NO_FIX</solution>
<file>
{file}
</file>

Previous response:
{bad_response}
"""


def is_valid_solution(text: str, file_context: str) -> bool:
    """Check whether model output contains valid solution blocks."""
    text = normalize_response(text)
    allowed_paths = set(source_paths(file_context))
    blocks = SOLUTION_BLOCK_RE.findall(text)
    if not text.startswith("<solution>") or not allowed_paths or not blocks:
        return False

    leftover = SOLUTION_BLOCK_RE.sub("", text)
    leftover = re.sub(r"\s*----\s*", "", leftover)
    if leftover.strip():
        return False

    for solution, file, summary in blocks:
        solution = solution.strip()
        file = file.strip()
        if not solution or file not in allowed_paths:
            return False
        if solution != "NO_FIX" and not (summary or "").strip():
            return False
        if solution.startswith("```") or solution.startswith("diff --git"):
            return False

    return True


def final_solution(file_context: str, response: str, retry=None) -> str:
    """Return valid solution output or a NO_FIX fallback."""
    response = normalize_response(response or "")
    if is_valid_solution(response, file_context):
        return response

    logging.warning("Model returned malformed solution output; retrying strict format repair.")
    if retry:
        repaired = normalize_response(retry(response) or "")
        if is_valid_solution(repaired, file_context):
            return repaired

    logging.warning("Model did not produce valid solution blocks; returning NO_FIX.")
    return no_fix_solution(file_context)


class AbstractProvider(ABC):
    def __init__(
        self,
        context: str | None = None,
        *,
        config: Config | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        """Initialize provider with context and model settings."""
        self.context = context
        self.config = config or get_config()
        self.ai_api_key = self.config.ai_api_key if api_key is None else api_key
        self.model = self.config.ai_model if model is None else model

    @property
    @abstractmethod
    def client(self):
        """Return provider client instance."""
        ...

    @abstractmethod
    def send_for_llm(self, file_context: str) -> str:
        """Sends the file name where the error occurs and returns a response"""
        ...

    @abstractmethod
    def get_models_list(self) -> list[str]:
        """Provides list of models of current provider"""
        ...

    def review_changes(self, review_context: str) -> str:
        """Review changed code and return strict review output."""
        raise NotImplementedError

    def get_solution(self) -> str:
        """Gets the final response that contains presumably solution"""
        files = get_error_files_from_dbt_log(self.context)
        if not files and self.config.uploaded_dbt_log.exists():
            try:
                files = get_error_files_from_dbt_log(
                    self.config.uploaded_dbt_log.read_text(encoding="utf-8", errors="replace")
                )
            except OSError as exc:
                logging.warning("Unable to read uploaded dbt log for file detection: %s", exc)

        if files:
            logging.info("Files selected from dbt log: %s", files)
        else:
            logging.warning("No dbt error file found in logs; skipping AI fix generation.")
            return ""

        files = list(dict.fromkeys(files))
        results = []

        for file in files:
            file_ctx = get_file_context(file)
            if not file_ctx:
                logging.warning("No file context found for: %s", file)
                continue
            logging.info("File context for %s:\n%s", file, file_ctx)
            results.append(self.send_for_llm(file_ctx))

        return "\n----\n".join(results)


class GoogleAIProvider(AbstractProvider):
    @property
    def client(self):
        """Return Google GenAI client."""
        return genai.Client(api_key=self.ai_api_key)

    def _generate(self, instruction: str, content: str) -> str:
        """Generate content with Google AI."""
        response = retry_request(
            lambda: self.client.models.generate_content(
                model=self.model,
                config=types.GenerateContentConfig(system_instruction=instruction),
                contents=content,
            )
        )
        return response.text or ""

    def send_for_llm(self, file_context: str) -> str:
        """Send source context to Google AI and normalize solution."""
        response = self._generate(
            get_instruction("handle_solution"),
            build_solution_prompt(self.context, file_context),
        )
        return final_solution(
            file_context,
            response,
            lambda bad_response: self._generate(
                "You repair output format only. Return tags only.",
                repair_prompt(file_context, bad_response),
            ),
        )

    def review_changes(self, review_context: str) -> str:
        """Review changed code with Google AI."""
        return self._generate(
            get_instruction("handle_review"),
            build_review_prompt(review_context),
        )

    def get_models_list(self):
        """Return available Google AI model names."""
        return [model.name for model in self.client.models.list() if model.name]


class BaseChatProvider(AbstractProvider):
    @abstractmethod
    def _chat(self, messages: list[dict[str, str]]) -> str:
        """Send chat messages to provider."""
        ...

    def _solution_messages(self, file_context: str) -> list[dict[str, str]]:
        """Build messages for solution generation."""
        return [
            {
                "role": "system",
                "content": get_instruction("handle_solution"),
            },
            {
                "role": "user",
                "content": build_solution_prompt(self.context, file_context),
            },
        ]

    def _repair_messages(self, file_context: str, bad_response: str) -> list[dict[str, str]]:
        """Build messages for solution format repair."""
        return [
            {
                "role": "system",
                "content": "You repair output format only. Return tags only.",
            },
            {
                "role": "user",
                "content": repair_prompt(file_context, bad_response),
            },
        ]

    def _review_messages(self, review_context: str) -> list[dict[str, str]]:
        """Build messages for change review."""
        return [
            {
                "role": "system",
                "content": get_instruction("handle_review"),
            },
            {
                "role": "user",
                "content": build_review_prompt(review_context),
            },
        ]

    def send_for_llm(self, file_context: str) -> str:
        """Send source context to chat provider and normalize solution."""
        response = retry_request(lambda: self._chat(self._solution_messages(file_context)))
        return final_solution(
            file_context,
            response,
            lambda bad_response: retry_request(
                lambda: self._chat(self._repair_messages(file_context, bad_response))
            ),
        )

    def review_changes(self, review_context: str) -> str:
        """Review changed code with chat provider."""
        return retry_request(lambda: self._chat(self._review_messages(review_context)))


class DeepSeekProvider(BaseChatProvider):
    base_url = "https://api.deepseek.com"
    fallback_models = ["deepseek-v4-flash", "deepseek-v4-pro"]

    @property
    def client(self):
        """Return configured DeepSeek HTTP session."""
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {self.ai_api_key}",
            "Content-Type": "application/json",
        })
        return session

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Perform DeepSeek API request."""
        response = self.client.request(
            method=method,
            url=f"{self.base_url}{path}",
            timeout=120,
            **kwargs,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text.strip()
            if detail:
                raise RuntimeError(f"DeepSeek API request failed: {detail}") from exc
            raise

        return response.json()

    def _chat(self, messages: list[dict[str, str]]) -> str:
        """Send chat completion request to DeepSeek."""
        data = self._request(
            "POST",
            "/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "temperature": 0,
            },
        )
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("DeepSeek API response did not include choices.")

        message = choices[0].get("message") or {}
        return message.get("content") or ""

    def get_models_list(self) -> list[str]:
        """Return available DeepSeek model names."""
        if not self.ai_api_key:
            return self.fallback_models

        try:
            data = self._request("GET", "/models")
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            logging.warning("Unable to list DeepSeek models: %s", exc)
            return self.fallback_models

        models = [model["id"] for model in data.get("data", []) if model.get("id")]
        return models or self.fallback_models


class BaseOllamaProvider(BaseChatProvider):
    def _chat(self, messages: list[dict[str, str]]) -> str:
        """Send chat request to Ollama."""
        response = self.client.chat(model=self.model, messages=messages)
        return response.message.content or ""

    def get_models_list(self) -> list[str]:
        """Return available Ollama model names."""
        return [model["model"] for model in self.client.list()["models"] if model["model"]]


class LocalOllamaProvider(BaseOllamaProvider):
    def _limit_text(self, text: str) -> str:
        """Limit local Ollama prompt size."""
        max_chars = self.config.ai_max_input_chars
        if not max_chars or max_chars <= 0 or len(text) <= max_chars:
            return text

        marker = (
            f"\n\n[... truncated {len(text) - max_chars} chars "
            "to keep the local Ollama prompt within the configured input limit ...]\n\n"
        )
        budget = max_chars - len(marker)
        if budget <= 0:
            return text[:max_chars]

        head = budget // 2
        tail = budget - head
        logging.warning(f"Truncating local Ollama prompt from {len(text)} to {max_chars} characters")
        return text[:head] + marker + text[-tail:]

    def _limit_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Limit user messages for local Ollama."""
        limited = []
        for message in messages:
            if message.get("role") == "user":
                message = {**message, "content": self._limit_text(message.get("content", ""))}
            limited.append(message)
        return limited

    def _chat(self, messages: list[dict[str, str]]):
        """Send chat request to local Ollama."""
        options = {"temperature": 0}
        if self.config.ollama_num_ctx and self.config.ollama_num_ctx > 0:
            options["num_ctx"] = self.config.ollama_num_ctx

        kwargs = {
            "model": self.model,
            "messages": self._limit_messages(messages),
            "options": options,
        }

        response = self.client.chat(**kwargs)
        return response.message.content or ""

    @property
    def client(self):
        """Return local Ollama client."""
        if self.config.ollama_host:
            return Client(host=self.config.ollama_host)
        return Client()


class APIOllamaProvider(BaseOllamaProvider):
    @property
    def client(self):
        """Return Ollama cloud API client."""
        return Client(
            host="https://ollama.com",
            headers={'Authorization': 'Bearer ' + self.ai_api_key}
        )

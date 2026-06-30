#!/usr/bin/env python3
"""Semi-automatic helper for XMUOJ contest practice.

The helper logs in, enters a password-protected contest, fetches problem
statements, prepares prompts, submits code, polls judge results, and keeps a
small local progress file. Secrets are read from environment variables or
interactive prompts; they are not written to disk.
"""

from __future__ import annotations

import argparse
import getpass
import html
import json
import os
import re
import secrets
import shutil
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_BASE_URL = "http://xmuoj.com"
DEFAULT_CONTEST_ID = "359"
STATE_DIR = Path(".xmuoj")
PROBLEMS_DIR = STATE_DIR / "problems"
PROMPTS_DIR = STATE_DIR / "prompts"
SOLUTIONS_DIR = STATE_DIR / "solutions"
SUBMISSIONS_DIR = STATE_DIR / "submissions"
MODEL_OUTPUTS_DIR = STATE_DIR / "model_outputs"
PROGRESS_PATH = STATE_DIR / "progress.json"
PROBLEM_LIST_PATH = STATE_DIR / "problem_list.json"
DEFAULT_SOLVER_API_BASE = "https://api.deepseek.com"
DEFAULT_SOLVER_MODEL = "deepseek-v4-pro"

RESULT_NAMES = {
    -2: "Compile Error",
    -1: "Wrong Answer",
    0: "Accepted",
    1: "Time Limit Exceeded",
    2: "Time Limit Exceeded",
    3: "Memory Limit Exceeded",
    4: "Runtime Error",
    5: "System Error",
    6: "Pending",
    7: "Judging",
    8: "Partial Accepted",
    9: "Submitting",
}


class APIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class HTMLTextExtractor(HTMLParser):
    """Tiny HTML-to-text converter tuned for OJ statements."""

    BLOCK_TAGS = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.BLOCK_TAGS:
            self._newline()
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            self.href_stack.append(href)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            href = self.href_stack.pop() if self.href_stack else None
            if href:
                self.parts.append(f" ({href})")
        if tag in self.BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in (STATE_DIR, PROBLEMS_DIR, PROMPTS_DIR, SOLUTIONS_DIR, SUBMISSIONS_DIR, MODEL_OUTPUTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return value or "problem"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        path.write_text(text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def strip_for_preview(value: str, limit: int = 500) -> str:
    value = value.replace("\r", "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def extract_code(value: str) -> str:
    """Extract model-produced source code from a fenced or plain response."""

    value = value.strip()
    fence = re.search(r"```(?:cpp|c\+\+|cc|cxx|C\+\+|CPP)?\s*\n(.*?)```", value, re.DOTALL)
    if fence:
        return fence.group(1).strip() + "\n"
    if value.startswith("```") and value.endswith("```"):
        return value.strip("`").strip() + "\n"

    lines = value.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip() + "\n"


def result_name(result: Any) -> str:
    try:
        key = int(result)
    except (TypeError, ValueError):
        return f"Unknown({result})"
    return RESULT_NAMES.get(key, f"Unknown({key})")


def submission_score(submission: dict[str, Any]) -> Any:
    for key in ("score", "total_score"):
        if key in submission:
            return submission[key]
    statistic = submission.get("statistic_info") or {}
    if isinstance(statistic, dict):
        for key in ("score", "total_score"):
            if key in statistic:
                return statistic[key]
    info = submission.get("info") or {}
    rows = info.get("data") if isinstance(info, dict) else None
    if isinstance(rows, list):
        scores = [row.get("score") for row in rows if isinstance(row, dict) and row.get("score") is not None]
        if scores:
            return sum(scores)
    return None


def format_submission_result(submission: dict[str, Any]) -> str:
    result = submission.get("result")
    pieces = [result_name(result)]
    score = submission_score(submission)
    if score is not None:
        pieces.append(f"score={score}")
    cpu_time = submission.get("cpu_time")
    memory = submission.get("memory")
    if cpu_time is not None:
        pieces.append(f"time={cpu_time}ms")
    if memory is not None:
        pieces.append(f"memory={memory}")
    submission_id = submission.get("id")
    if submission_id is not None:
        pieces.append(f"id={submission_id}")
    return ", ".join(pieces)


def is_submission_done(submission: dict[str, Any]) -> bool:
    try:
        result = int(submission.get("result"))
    except (TypeError, ValueError):
        return False
    if result in (6, 7, 9):
        return False
    statistic = submission.get("statistic_info")
    return bool(statistic) or result in (-2, -1, 0, 1, 2, 3, 4, 5, 8)


def is_submission_accepted(submission: dict[str, Any]) -> bool:
    try:
        return int(submission.get("result")) == 0
    except (TypeError, ValueError):
        return False


@dataclass
class Credentials:
    username: str
    password: str
    contest_password: str


class XMUOJClient:
    def __init__(self, base_url: str, contest_id: str, timeout: int = 25) -> None:
        self.base_url = base_url.rstrip("/")
        self.contest_id = str(contest_id)
        self.timeout = timeout
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def login(self, username: str, password: str) -> None:
        self.post("login", {"username": username, "password": password})

    def enter_contest(self, contest_password: str) -> bool:
        data = self.post(
            "contest/password",
            {"contest_id": int(self.contest_id), "password": contest_password},
        )
        return bool(data)

    def contest_access(self) -> bool:
        data = self.get("contest/access", {"contest_id": self.contest_id})
        return bool(data.get("access")) if isinstance(data, dict) else False

    def get_contest(self) -> dict[str, Any]:
        return self.get("contest", {"id": self.contest_id})

    def get_problem_list(self) -> list[dict[str, Any]]:
        data = self.get("contest/problem", {"contest_id": self.contest_id})
        if not isinstance(data, list):
            raise APIError(f"Unexpected problem list payload: {type(data).__name__}")
        return data

    def get_problem(self, display_id: str) -> dict[str, Any]:
        return self.get(
            "contest/problem",
            {"contest_id": self.contest_id, "problem_id": display_id},
        )

    def submission_exists(self, internal_problem_id: int | str) -> bool:
        data = self.get(
            "submission_exists",
            {"contest_id": self.contest_id, "problem_id": internal_problem_id},
        )
        return bool(data)

    def submit_code(
        self,
        *,
        internal_problem_id: int | str,
        language: str,
        code: str,
        captcha: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "problem_id": internal_problem_id,
            "language": language,
            "code": code,
            "contest_id": self.contest_id,
        }
        if captcha:
            payload["captcha"] = captcha
        data = self.post("submission", payload)
        if not isinstance(data, dict) or "submission_id" not in data:
            raise APIError(f"Unexpected submit payload: {data!r}")
        return str(data["submission_id"])

    def get_submission(self, submission_id: int | str) -> dict[str, Any]:
        return self.get("submission", {"id": submission_id})

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, data: dict[str, Any]) -> Any:
        return self._request("POST", path, data=data)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {
            "Accept": "application/json",
            "User-Agent": "xmuoj-helper/1.0",
        }
        body: bytes | None = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"

        if method.upper() != "GET":
            headers.update(
                {
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/contest/{self.contest_id}",
                    "X-CSRFToken": self._csrf_token(),
                    "X-Requested-With": "XMLHttpRequest",
                }
            )

        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise APIError(f"HTTP {exc.code}: {strip_for_preview(text)}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise APIError(f"Network error: {exc}") from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise APIError(f"Non-JSON response: {strip_for_preview(text)}") from exc

        if payload.get("error") is not None:
            raise APIError(str(payload.get("data") or payload.get("error")), payload=payload)
        return payload.get("data")

    def _csrf_token(self) -> str:
        token = self._cookie_value("csrftoken")
        if token:
            return token
        token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(64))
        self._set_cookie("csrftoken", token)
        return token

    def _cookie_value(self, name: str) -> str | None:
        host = urllib.parse.urlparse(self.base_url).hostname or "xmuoj.com"
        for cookie in self.cookies:
            if cookie.name == name and (cookie.domain == host or cookie.domain.endswith(host)):
                return cookie.value
        return None

    def _set_cookie(self, name: str, value: str) -> None:
        host = urllib.parse.urlparse(self.base_url).hostname or "xmuoj.com"
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=host,
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        self.cookies.set_cookie(cookie)


class OpenAICompatibleSolver:
    """Minimal OpenAI-compatible chat completions client."""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
        thinking: str = "enabled",
        reasoning_effort: str = "medium",
    ) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort

    def solve(self, prompt: str) -> str:
        return self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are an expert competitive programmer. "
                        "Return only one complete C++17 solution. "
                        "Do not include explanations or Markdown fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )

    def repair(self, prompt: str) -> str:
        return self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You fix competitive programming submissions. "
                        "Return only one complete corrected C++17 solution. "
                        "Do not include explanations or Markdown fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )

    def _complete(self, messages: list[dict[str, str]]) -> str:
        url = f"{self.api_base}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.thinking == "enabled":
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        elif self.thinking == "disabled":
            payload["thinking"] = {"type": "disabled"}

        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "xmuoj-helper/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise APIError(f"Solver HTTP {exc.code}: {strip_for_preview(text)}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise APIError(f"Solver network error: {exc}") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise APIError(f"Solver returned non-JSON response: {strip_for_preview(text)}") from exc

        if data.get("error"):
            raise APIError(f"Solver error: {data['error']}", payload=data)
        choices = data.get("choices") or []
        if not choices:
            raise APIError(f"Solver returned no choices: {strip_for_preview(text)}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise APIError(f"Solver returned empty content: {strip_for_preview(text)}")
        return str(content)


class ProgressStore:
    def __init__(self, contest_id: str) -> None:
        self.contest_id = str(contest_id)
        self.data = read_json(
            PROGRESS_PATH,
            {
                "contest_id": self.contest_id,
                "updated_at": utc_now(),
                "problems": {},
            },
        )
        self.data.setdefault("problems", {})

    def save(self) -> None:
        self.data["contest_id"] = self.contest_id
        self.data["updated_at"] = utc_now()
        write_json(PROGRESS_PATH, self.data)

    def problem(self, display_id: str) -> dict[str, Any]:
        problems = self.data.setdefault("problems", {})
        return problems.setdefault(display_id, {})

    def update_problem(self, display_id: str, **fields: Any) -> None:
        entry = self.problem(display_id)
        entry.update(fields)
        entry["updated_at"] = utc_now()
        self.save()

    def status(self, display_id: str) -> str:
        return str(self.problem(display_id).get("status") or "new")


def render_samples(samples: Any) -> str:
    if not samples:
        return "_The API returned no samples._"
    if not isinstance(samples, list):
        return f"```text\n{samples}\n```"

    blocks: list[str] = []
    for index, sample in enumerate(samples, 1):
        if isinstance(sample, dict):
            sample_input = (
                sample.get("input")
                or sample.get("sample_input")
                or sample.get("stdin")
                or sample.get("in")
                or ""
            )
            sample_output = (
                sample.get("output")
                or sample.get("sample_output")
                or sample.get("stdout")
                or sample.get("out")
                or ""
            )
            blocks.append(
                "\n".join(
                    [
                        f"Sample {index} Input:",
                        "```text",
                        str(sample_input).rstrip(),
                        "```",
                        f"Sample {index} Output:",
                        "```text",
                        str(sample_output).rstrip(),
                        "```",
                    ]
                )
            )
        else:
            blocks.append(f"Sample {index}:\n```text\n{sample}\n```")
    return "\n\n".join(blocks)


def problem_markdown(problem: dict[str, Any]) -> str:
    display_id = str(problem.get("_id") or problem.get("id") or "")
    title = str(problem.get("title") or "")
    tags = ", ".join(str(item) for item in problem.get("tags") or [])
    languages = ", ".join(str(item) for item in problem.get("languages") or [])
    io_mode = problem.get("io_mode") or {}
    if isinstance(io_mode, dict):
        io_text = io_mode.get("io_mode") or ""
    else:
        io_text = str(io_mode)

    sections = [
        f"# {display_id} {title}".strip(),
        "",
        "## Metadata",
        f"- Internal problem id: {problem.get('id')}",
        f"- Contest id: {problem.get('contest')}",
        f"- Rule type: {problem.get('rule_type')}",
        f"- Score: {problem.get('total_score')}",
        f"- Difficulty: {problem.get('difficulty')}",
        f"- Time limit: {problem.get('time_limit')} ms",
        f"- Memory limit: {problem.get('memory_limit')} MB",
        f"- IO mode: {io_text}",
        f"- Languages: {languages}",
        f"- Source: {problem.get('source')}",
        f"- Tags: {tags}",
        f"- Accepted/Submissions: {problem.get('accepted_number')}/{problem.get('submission_number')}",
        "",
        "## Description",
        html_to_text(problem.get("description")),
        "",
        "## Input",
        html_to_text(problem.get("input_description")),
        "",
        "## Output",
        html_to_text(problem.get("output_description")),
        "",
        "## Samples",
        render_samples(problem.get("samples")),
        "",
        "## Hint",
        html_to_text(problem.get("hint")),
        "",
    ]
    return "\n".join(str(item) for item in sections).strip() + "\n"


def solution_prompt(problem: dict[str, Any]) -> str:
    statement = problem_markdown(problem).strip()
    return (
        "请用 C++17 解下面这道 OI 题。\n"
        "要求：\n"
        "- 只输出完整 C++17 代码，不要解释。\n"
        "- 使用标准输入输出，不要使用文件 IO。\n"
        "- 注意隐藏测试，给出鲁棒的边界处理。\n"
        "- 如果题面样例为空，也要根据题意完整求解。\n\n"
        f"{statement}\n"
    )


def repair_prompt(problem: dict[str, Any], code: str, submission: dict[str, Any]) -> str:
    statement = problem_markdown(problem).strip()
    return (
        "请修复下面这道 OI 题的 C++17 解答。\n"
        "要求：只输出修复后的完整代码，不要解释。\n\n"
        f"判题结果：{format_submission_result(submission)}\n\n"
        f"{statement}\n\n"
        "上次提交代码：\n"
        "```cpp\n"
        f"{code.rstrip()}\n"
        "```\n"
    )


def problem_file_stem(problem: dict[str, Any]) -> str:
    display_id = safe_name(str(problem.get("_id") or problem.get("id") or "problem"))
    title = safe_name(str(problem.get("title") or ""))
    return f"{display_id}_{title}" if title else display_id


def write_problem_artifacts(problem: dict[str, Any]) -> dict[str, Path]:
    ensure_dirs()
    display_id = str(problem.get("_id") or problem.get("id"))
    stem = problem_file_stem(problem)
    problem_path = PROBLEMS_DIR / f"{stem}.md"
    prompt_path = PROMPTS_DIR / f"{stem}_prompt.md"
    solution_path = SOLUTIONS_DIR / f"{safe_name(display_id)}.cpp"

    problem_path.write_text(problem_markdown(problem), encoding="utf-8")
    prompt_path.write_text(solution_prompt(problem), encoding="utf-8")
    if not solution_path.exists():
        solution_path.write_text(
            "#include <bits/stdc++.h>\n"
            "using namespace std;\n\n"
            "int main() {\n"
            "    ios::sync_with_stdio(false);\n"
            "    cin.tie(nullptr);\n\n"
            "    return 0;\n"
            "}\n",
            encoding="utf-8",
        )
    return {
        "problem": problem_path,
        "prompt": prompt_path,
        "solution": solution_path,
    }


def write_repair_artifact(problem: dict[str, Any], code: str, submission: dict[str, Any]) -> Path:
    ensure_dirs()
    stem = problem_file_stem(problem)
    path = PROMPTS_DIR / f"{stem}_repair.md"
    path.write_text(repair_prompt(problem, code, submission), encoding="utf-8")
    return path


def load_credentials(args: argparse.Namespace) -> Credentials:
    username = args.username or os.environ.get("XMUOJ_USERNAME")
    if not username:
        username = input("XMUOJ username: ").strip()
    password = os.environ.get("XMUOJ_PASSWORD")
    if not password:
        password = getpass.getpass("XMUOJ account password: ")
    contest_password = os.environ.get("XMUOJ_CONTEST_PASSWORD")
    if not contest_password:
        contest_password = getpass.getpass("Contest password: ")
    return Credentials(username=username, password=password, contest_password=contest_password)


def authenticate(args: argparse.Namespace) -> XMUOJClient:
    credentials = load_credentials(args)
    client = XMUOJClient(args.base_url, args.contest_id, timeout=args.timeout)
    print("Logging in...")
    client.login(credentials.username, credentials.password)
    print("Entering contest...")
    client.enter_contest(credentials.contest_password)
    if not client.contest_access():
        raise APIError("Contest access is still false after password submission.")
    return client


def build_solver(args: argparse.Namespace) -> OpenAICompatibleSolver:
    key_env = args.solver_api_key_env
    api_key = os.environ.get(key_env)
    if not api_key:
        api_key = getpass.getpass(f"{key_env}: ")
    return OpenAICompatibleSolver(
        api_key=api_key,
        api_base=args.solver_api_base,
        model=args.solver_model,
        temperature=args.solver_temperature,
        max_tokens=args.solver_max_tokens,
        timeout=args.solver_timeout,
        thinking=args.solver_thinking,
        reasoning_effort=args.solver_reasoning_effort,
    )


def save_model_output(
    *,
    display_id: str,
    attempt: int,
    stage: str,
    raw_text: str,
    code: str,
) -> dict[str, Path]:
    ensure_dirs()
    stem = f"{safe_name(display_id)}_attempt{attempt}_{safe_name(stage)}"
    raw_path = MODEL_OUTPUTS_DIR / f"{stem}_raw.txt"
    code_path = MODEL_OUTPUTS_DIR / f"{stem}.cpp"
    raw_path.write_text(raw_text.rstrip() + "\n", encoding="utf-8")
    code_path.write_text(code, encoding="utf-8")
    return {"raw": raw_path, "code": code_path}


def sync_problem_list(client: XMUOJClient, progress: ProgressStore) -> list[dict[str, Any]]:
    problems = client.get_problem_list()
    write_json(PROBLEM_LIST_PATH, problems)
    for problem in problems:
        display_id = str(problem.get("_id") or problem.get("id"))
        progress.update_problem(
            display_id,
            title=problem.get("title"),
            list_internal_id=problem.get("id"),
            accepted_number=problem.get("accepted_number"),
            submission_number=problem.get("submission_number"),
        )
    return problems


def cached_problem_list() -> list[dict[str, Any]]:
    return read_json(PROBLEM_LIST_PATH, [])


def find_problem(problems: list[dict[str, Any]], display_id: str) -> dict[str, Any]:
    for problem in problems:
        if str(problem.get("_id") or problem.get("id")) == str(display_id):
            return problem
    raise APIError(f"Problem {display_id} was not found in the contest list.")


def find_next_problem(problems: list[dict[str, Any]], progress: ProgressStore) -> dict[str, Any] | None:
    for problem in problems:
        display_id = str(problem.get("_id") or problem.get("id"))
        if progress.status(display_id) not in {"accepted", "skipped"}:
            return problem
    return None


def get_problem_detail(client: XMUOJClient, progress: ProgressStore, display_id: str) -> dict[str, Any]:
    problem = client.get_problem(display_id)
    progress.update_problem(
        display_id,
        title=problem.get("title"),
        internal_id=problem.get("id"),
        total_score=problem.get("total_score"),
        source=problem.get("source"),
    )
    return problem


def poll_submission(
    client: XMUOJClient,
    submission_id: int | str,
    *,
    interval: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_line = ""
    while True:
        submission = client.get_submission(submission_id)
        line = format_submission_result(submission)
        if line != last_line:
            print(f"Judge: {line}")
            last_line = line
        if is_submission_done(submission):
            return submission
        if time.monotonic() >= deadline:
            raise APIError(f"Timed out waiting for submission {submission_id}.")
        time.sleep(interval)


def submit_solution(
    client: XMUOJClient,
    progress: ProgressStore,
    *,
    display_id: str,
    code_path: Path,
    language: str,
    interval: float,
    timeout: float,
) -> dict[str, Any]:
    problem = get_problem_detail(client, progress, display_id)
    code = code_path.read_text(encoding="utf-8")
    if not code.strip():
        raise APIError(f"Code file is empty: {code_path}")

    print(f"Submitting {display_id} {problem.get('title')} as {language}...")
    submission_id = client.submit_code(
        internal_problem_id=problem["id"],
        language=language,
        code=code,
    )
    print(f"Submission id: {submission_id}")
    submitted_copy = SUBMISSIONS_DIR / f"{safe_name(display_id)}_{submission_id}.cpp"
    submitted_copy.write_text(code, encoding="utf-8")

    submission = poll_submission(client, submission_id, interval=interval, timeout=timeout)
    accepted = is_submission_accepted(submission)
    status = "accepted" if accepted else "failed"
    progress.update_problem(
        display_id,
        status=status,
        submission_id=submission_id,
        result=submission.get("result"),
        result_name=result_name(submission.get("result")),
        score=submission_score(submission),
        code_path=str(code_path),
        submitted_copy=str(submitted_copy),
    )
    if accepted:
        print(f"{display_id} accepted.")
    else:
        repair_path = write_repair_artifact(problem, code, submission)
        print(f"{display_id} not accepted. Repair prompt: {repair_path}")
    return submission


def command_sync(args: argparse.Namespace) -> None:
    ensure_dirs()
    client = authenticate(args)
    progress = ProgressStore(args.contest_id)
    contest = client.get_contest()
    problems = sync_problem_list(client, progress)
    print(f"Contest: {contest.get('title')}")
    print(f"Problems: {len(problems)}")
    next_problem = find_next_problem(problems, progress)
    if next_problem:
        print(f"Next: {next_problem.get('_id')} {next_problem.get('title')}")
    else:
        print("All cached problems are marked accepted.")


def command_list(args: argparse.Namespace) -> None:
    ensure_dirs()
    client = authenticate(args)
    progress = ProgressStore(args.contest_id)
    problems = sync_problem_list(client, progress)
    for index, problem in enumerate(problems, 1):
        display_id = str(problem.get("_id") or problem.get("id"))
        print(f"{index:03d}. {display_id:<8} {problem.get('title')} [{progress.status(display_id)}]")


def command_prompt(args: argparse.Namespace) -> None:
    ensure_dirs()
    client = authenticate(args)
    progress = ProgressStore(args.contest_id)
    problems = sync_problem_list(client, progress)
    if args.problem_id:
        target = find_problem(problems, args.problem_id)
    else:
        target = find_next_problem(problems, progress)
        if not target:
            print("No next problem; all cached problems are accepted.")
            return
    display_id = str(target.get("_id") or target.get("id"))
    problem = get_problem_detail(client, progress, display_id)
    paths = write_problem_artifacts(problem)
    progress.update_problem(
        display_id,
        status=progress.status(display_id),
        problem_path=str(paths["problem"]),
        prompt_path=str(paths["prompt"]),
        solution_path=str(paths["solution"]),
    )
    print(f"Problem: {display_id} {problem.get('title')}")
    print(f"Statement: {paths['problem']}")
    print(f"Prompt:    {paths['prompt']}")
    print(f"Code file: {paths['solution']}")


def command_submit(args: argparse.Namespace) -> None:
    ensure_dirs()
    client = authenticate(args)
    progress = ProgressStore(args.contest_id)
    problems = sync_problem_list(client, progress)
    if args.problem_id:
        display_id = args.problem_id
    else:
        target = find_next_problem(problems, progress)
        if not target:
            print("No next problem; all cached problems are accepted.")
            return
        display_id = str(target.get("_id") or target.get("id"))
    code_path = Path(args.code) if args.code else SOLUTIONS_DIR / f"{safe_name(display_id)}.cpp"
    if not code_path.exists():
        raise APIError(f"Code file does not exist: {code_path}")
    submit_solution(
        client,
        progress,
        display_id=display_id,
        code_path=code_path,
        language=args.language,
        interval=args.poll_interval,
        timeout=args.poll_timeout,
    )


def command_poll(args: argparse.Namespace) -> None:
    client = authenticate(args)
    submission = poll_submission(
        client,
        args.submission_id,
        interval=args.poll_interval,
        timeout=args.poll_timeout,
    )
    print(json.dumps(submission, ensure_ascii=False, indent=2))


def command_interactive(args: argparse.Namespace) -> None:
    ensure_dirs()
    client = authenticate(args)
    progress = ProgressStore(args.contest_id)
    problems = sync_problem_list(client, progress)
    print(f"Loaded {len(problems)} problems.")

    current: dict[str, Any] | None = None
    while True:
        if current is None:
            current = find_next_problem(problems, progress)
        if current is None:
            print("All cached problems are marked accepted.")
            return

        display_id = str(current.get("_id") or current.get("id"))
        problem = get_problem_detail(client, progress, display_id)
        paths = write_problem_artifacts(problem)
        progress.update_problem(
            display_id,
            status=progress.status(display_id),
            problem_path=str(paths["problem"]),
            prompt_path=str(paths["prompt"]),
            solution_path=str(paths["solution"]),
        )

        print()
        print(f"Current: {display_id} {problem.get('title')} [{progress.status(display_id)}]")
        print(f"Prompt:  {paths['prompt']}")
        print(f"Code:    {paths['solution']}")
        print("Commands: submit, paste, skip, list, refresh, quit")
        command = input("> ").strip().lower()

        if command in {"q", "quit", "exit"}:
            return
        if command in {"l", "list"}:
            for index, item in enumerate(problems, 1):
                item_id = str(item.get("_id") or item.get("id"))
                print(f"{index:03d}. {item_id:<8} {item.get('title')} [{progress.status(item_id)}]")
            continue
        if command in {"r", "refresh"}:
            problems = sync_problem_list(client, progress)
            current = None
            continue
        if command in {"k", "skip", "next"}:
            progress.update_problem(display_id, status="skipped")
            current = None
            continue
        if command in {"p", "paste"}:
            print("Paste code now. End with a line that contains only ###END###")
            lines: list[str] = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if line.strip() == "###END###":
                    break
                lines.append(line)
            paths["solution"].write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            print(f"Wrote {paths['solution']}")
            command = "submit"
        if command in {"s", "submit"}:
            submission = submit_solution(
                client,
                progress,
                display_id=display_id,
                code_path=paths["solution"],
                language=args.language,
                interval=args.poll_interval,
                timeout=args.poll_timeout,
            )
            if is_submission_accepted(submission):
                current = None
            continue

        print("Unknown command.")


def auto_targets(
    problems: list[dict[str, Any]],
    progress: ProgressStore,
    *,
    problem_id: str | None,
    max_problems: int,
) -> list[dict[str, Any]]:
    if problem_id:
        return [find_problem(problems, problem_id)]
    targets: list[dict[str, Any]] = []
    for problem in problems:
        display_id = str(problem.get("_id") or problem.get("id"))
        if progress.status(display_id) in {"accepted", "skipped"}:
            continue
        targets.append(problem)
        if len(targets) >= max_problems:
            break
    return targets


def auto_solve_problem(
    *,
    client: XMUOJClient,
    solver: OpenAICompatibleSolver,
    progress: ProgressStore,
    display_id: str,
    language: str,
    submit: bool,
    max_retries: int,
    poll_interval: float,
    poll_timeout: float,
) -> str:
    problem = get_problem_detail(client, progress, display_id)
    paths = write_problem_artifacts(problem)
    progress.update_problem(
        display_id,
        status=progress.status(display_id),
        problem_path=str(paths["problem"]),
        prompt_path=str(paths["prompt"]),
        solution_path=str(paths["solution"]),
    )

    prompt = solution_prompt(problem)
    last_submission: dict[str, Any] | None = None
    for attempt in range(max_retries + 1):
        stage = "solve" if attempt == 0 else "repair"
        print(f"[{display_id}] model {stage} attempt {attempt + 1}/{max_retries + 1}...")
        raw_text = solver.solve(prompt) if attempt == 0 else solver.repair(prompt)
        code = extract_code(raw_text)
        paths["solution"].write_text(code, encoding="utf-8")
        output_paths = save_model_output(
            display_id=display_id,
            attempt=attempt + 1,
            stage=stage,
            raw_text=raw_text,
            code=code,
        )
        progress.update_problem(
            display_id,
            status="generated" if not submit else "submitting",
            generated_at=utc_now(),
            generated_code_path=str(output_paths["code"]),
            generated_raw_path=str(output_paths["raw"]),
            solution_path=str(paths["solution"]),
            auto_attempt=attempt + 1,
        )
        print(f"[{display_id}] wrote code: {paths['solution']}")

        if not submit:
            print(f"[{display_id}] dry-run mode: not submitting. Add --submit to send to OJ.")
            return "generated"

        last_submission = submit_solution(
            client,
            progress,
            display_id=display_id,
            code_path=paths["solution"],
            language=language,
            interval=poll_interval,
            timeout=poll_timeout,
        )
        if is_submission_accepted(last_submission):
            return "accepted"

        if attempt < max_retries:
            prompt = repair_prompt(problem, code, last_submission)
            repair_path = write_repair_artifact(problem, code, last_submission)
            progress.update_problem(display_id, repair_prompt_path=str(repair_path))
            print(f"[{display_id}] preparing repair attempt from {format_submission_result(last_submission)}")

    if last_submission is not None:
        progress.update_problem(display_id, status="failed")
    return "failed"


def command_auto(args: argparse.Namespace) -> None:
    ensure_dirs()
    client = authenticate(args)
    solver = build_solver(args)
    progress = ProgressStore(args.contest_id)
    problems = sync_problem_list(client, progress)
    targets = auto_targets(
        problems,
        progress,
        problem_id=args.problem_id,
        max_problems=args.max_problems,
    )
    if not targets:
        print("No target problems found.")
        return

    mode = "submit" if args.submit else "dry-run"
    print(f"Auto mode: {mode}, targets={len(targets)}, model={args.solver_model}")
    for index, problem in enumerate(targets, 1):
        display_id = str(problem.get("_id") or problem.get("id"))
        print()
        print(f"=== {index}/{len(targets)} {display_id} {problem.get('title')} ===")
        outcome = auto_solve_problem(
            client=client,
            solver=solver,
            progress=progress,
            display_id=display_id,
            language=args.language,
            submit=args.submit,
            max_retries=args.max_retries,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
        )
        if outcome == "failed" and not args.continue_on_failure:
            print("Stopping after failure. Use --continue-on-failure to keep going.")
            return
        if index < len(targets) and args.delay > 0:
            time.sleep(args.delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Semi-automatic helper for XMUOJ contest problems.",
    )
    parser.add_argument("--base-url", default=os.environ.get("XMUOJ_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--contest-id", default=os.environ.get("XMUOJ_CONTEST_ID", DEFAULT_CONTEST_ID))
    parser.add_argument("--username", default=os.environ.get("XMUOJ_USERNAME"))
    parser.add_argument("--language", default=os.environ.get("XMUOJ_LANGUAGE", "C++"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("XMUOJ_TIMEOUT", "25")))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("XMUOJ_POLL_INTERVAL", "2")))
    parser.add_argument("--poll-timeout", type=float, default=float(os.environ.get("XMUOJ_POLL_TIMEOUT", "120")))

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("sync", help="log in, enter contest, fetch and cache the problem list")
    subparsers.add_parser("list", help="show the contest problem list and local progress")

    prompt_parser = subparsers.add_parser("prompt", help="fetch a problem and write statement/prompt files")
    prompt_parser.add_argument("problem_id", nargs="?", help="display id such as JD001; defaults to next unsolved")

    submit_parser = subparsers.add_parser("submit", help="submit a local code file and poll the result")
    submit_parser.add_argument("problem_id", nargs="?", help="display id such as JD001; defaults to next unsolved")
    submit_parser.add_argument("--code", help="path to code file; defaults to .xmuoj/solutions/<problem_id>.cpp")

    poll_parser = subparsers.add_parser("poll", help="poll an existing submission id")
    poll_parser.add_argument("submission_id")

    auto_parser = subparsers.add_parser("auto", help="generate solutions with a model; dry-run unless --submit is set")
    auto_parser.add_argument("problem_id", nargs="?", help="display id such as JD001; defaults to next unsolved")
    auto_parser.add_argument("--submit", action="store_true", help="actually submit generated code to OJ")
    auto_parser.add_argument("--max-problems", type=int, default=int(os.environ.get("XMUOJ_AUTO_MAX_PROBLEMS", "1")))
    auto_parser.add_argument("--max-retries", type=int, default=int(os.environ.get("XMUOJ_AUTO_MAX_RETRIES", "2")))
    auto_parser.add_argument("--delay", type=float, default=float(os.environ.get("XMUOJ_AUTO_DELAY", "5")))
    auto_parser.add_argument("--continue-on-failure", action="store_true")
    auto_parser.add_argument("--solver", choices=["deepseek"], default=os.environ.get("XMUOJ_SOLVER", "deepseek"))
    auto_parser.add_argument(
        "--solver-api-key-env",
        default=os.environ.get("XMUOJ_SOLVER_API_KEY_ENV", "DEEPSEEK_API_KEY"),
        help="environment variable that contains the solver API key",
    )
    auto_parser.add_argument(
        "--solver-api-base",
        default=os.environ.get("DEEPSEEK_API_BASE", DEFAULT_SOLVER_API_BASE),
    )
    auto_parser.add_argument(
        "--solver-model",
        default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_SOLVER_MODEL),
    )
    auto_parser.add_argument(
        "--solver-temperature",
        type=float,
        default=float(os.environ.get("XMUOJ_SOLVER_TEMPERATURE", "0.2")),
    )
    auto_parser.add_argument(
        "--solver-max-tokens",
        type=int,
        default=int(os.environ.get("XMUOJ_SOLVER_MAX_TOKENS", "8192")),
    )
    auto_parser.add_argument(
        "--solver-timeout",
        type=int,
        default=int(os.environ.get("XMUOJ_SOLVER_TIMEOUT", "180")),
    )
    auto_parser.add_argument(
        "--solver-thinking",
        choices=["default", "enabled", "disabled"],
        default=os.environ.get("DEEPSEEK_THINKING", "enabled"),
    )
    auto_parser.add_argument(
        "--solver-reasoning-effort",
        choices=["low", "medium", "high"],
        default=os.environ.get("DEEPSEEK_REASONING_EFFORT", "medium"),
    )

    subparsers.add_parser("interactive", help="start the semi-automatic interactive loop")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "interactive"

    handlers = {
        "sync": command_sync,
        "list": command_list,
        "prompt": command_prompt,
        "submit": command_submit,
        "poll": command_poll,
        "auto": command_auto,
        "interactive": command_interactive,
    }

    try:
        handlers[command](args)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except APIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

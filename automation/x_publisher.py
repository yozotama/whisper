from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError


DEFAULT_CONFIG_PATH = "automation/example_config.json"
DEFAULT_OUTPUT_DIR = "automation/runs"
X_CREATE_TWEET_URL = "https://api.x.com/2/tweets"


class PipelineError(RuntimeError):
    """Raised when a pipeline step cannot continue safely."""


@dataclass
class FeedItem:
    title: str
    link: str
    published: str
    summary: str
    source_feed: str


@dataclass
class OpenAIConfig:
    api_key_env: str
    model: str
    base_url: str


@dataclass
class XConfig:
    api_key_env: str
    api_secret_env: str
    access_token_env: str
    access_token_secret_env: str


@dataclass
class AppConfig:
    topic: str
    instruction: str
    rss_urls: list[str]
    keywords: list[str]
    max_items: int
    language: str
    output_dir: Path
    request_timeout_seconds: int
    openai: OpenAIConfig
    x: XConfig


@dataclass
class DraftResult:
    title: str
    article_markdown: str
    x_post: str
    model_used: str
    used_fallback: bool


@dataclass
class XCredentials:
    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str


def load_config(config_path: str) -> AppConfig:
    path = Path(config_path).resolve()
    if not path.exists():
        raise PipelineError(f"Config file not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    rss_urls = raw.get("rss_urls", [])
    if not isinstance(rss_urls, list) or not rss_urls:
        raise PipelineError("`rss_urls` must be a non-empty list in config.")

    output_dir_raw = raw.get("output_dir", DEFAULT_OUTPUT_DIR)
    output_dir = Path(output_dir_raw)
    if not output_dir.is_absolute():
        output_dir = (path.parent / output_dir).resolve()

    openai_raw = raw.get("openai", {})
    x_raw = raw.get("x", {})

    return AppConfig(
        topic=str(raw.get("topic", "Tech updates")),
        instruction=str(
            raw.get(
                "instruction",
                "Summarize key points, explain why they matter, and propose actions.",
            )
        ),
        rss_urls=[str(url) for url in rss_urls],
        keywords=[str(k).strip() for k in raw.get("keywords", []) if str(k).strip()],
        max_items=max(int(raw.get("max_items", 8)), 1),
        language=str(raw.get("language", "ja")),
        output_dir=output_dir,
        request_timeout_seconds=max(int(raw.get("request_timeout_seconds", 20)), 5),
        openai=OpenAIConfig(
            api_key_env=str(openai_raw.get("api_key_env", "OPENAI_API_KEY")),
            model=str(openai_raw.get("model", "gpt-4.1-mini")),
            base_url=str(openai_raw.get("base_url", "https://api.openai.com/v1")),
        ),
        x=XConfig(
            api_key_env=str(x_raw.get("api_key_env", "X_API_KEY")),
            api_secret_env=str(x_raw.get("api_secret_env", "X_API_SECRET")),
            access_token_env=str(x_raw.get("access_token_env", "X_ACCESS_TOKEN")),
            access_token_secret_env=str(
                x_raw.get("access_token_secret_env", "X_ACCESS_TOKEN_SECRET")
            ),
        ),
    )


def create_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def read_url_text(url: str, timeout_seconds: int) -> str:
    req = request.Request(
        url,
        method="GET",
        headers={"User-Agent": "auto-x-publisher/0.1"},
    )
    with request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(elem: ET.Element, names: list[str]) -> str:
    lowered = {name.lower() for name in names}
    for child in list(elem):
        if _strip_ns(child.tag).lower() in lowered:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def parse_feed_xml(xml_text: str, source_feed: str) -> list[FeedItem]:
    root = ET.fromstring(xml_text)
    root_tag = _strip_ns(root.tag).lower()
    items: list[FeedItem] = []

    if root_tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return items
        for item_elem in channel.findall("item"):
            title = _first_text(item_elem, ["title"]) or "Untitled"
            link = _first_text(item_elem, ["link"])
            published = _first_text(item_elem, ["pubDate", "published"])
            summary = _first_text(item_elem, ["description", "summary"])
            items.append(
                FeedItem(
                    title=title,
                    link=link,
                    published=published,
                    summary=summary,
                    source_feed=source_feed,
                )
            )
        return items

    if root_tag == "feed":
        for entry in list(root):
            if _strip_ns(entry.tag).lower() != "entry":
                continue
            title = _first_text(entry, ["title"]) or "Untitled"
            published = _first_text(entry, ["published", "updated"])
            summary = _first_text(entry, ["summary", "content"])
            link = ""
            for child in list(entry):
                if _strip_ns(child.tag).lower() != "link":
                    continue
                href = child.attrib.get("href", "").strip()
                if href:
                    rel = child.attrib.get("rel", "").strip().lower()
                    if not rel or rel == "alternate":
                        link = href
                        break
            items.append(
                FeedItem(
                    title=title,
                    link=link,
                    published=published,
                    summary=summary,
                    source_feed=source_feed,
                )
            )
    return items


def collect_feed_items(config: AppConfig) -> tuple[list[FeedItem], list[str]]:
    collected: list[FeedItem] = []
    errors: list[str] = []

    for feed_url in config.rss_urls:
        try:
            xml_text = read_url_text(feed_url, timeout_seconds=config.request_timeout_seconds)
            collected.extend(parse_feed_xml(xml_text, source_feed=feed_url))
        except (ET.ParseError, HTTPError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"{feed_url}: {exc}")

    filtered: list[FeedItem] = []
    seen: set[str] = set()
    lowered_keywords = [k.lower() for k in config.keywords]

    for item in collected:
        dedupe_key = item.link.strip() or item.title.strip().lower()
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        if lowered_keywords:
            haystack = f"{item.title}\n{item.summary}".lower()
            if not any(keyword in haystack for keyword in lowered_keywords):
                continue

        filtered.append(item)
        if len(filtered) >= config.max_items:
            break

    return filtered, errors


def _json_from_maybe_markdown(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidate = stripped[start : end + 1]
        return json.loads(candidate)
    raise PipelineError("Model response did not contain a JSON object.")


def _build_openai_prompt(config: AppConfig, items: list[FeedItem]) -> str:
    source_lines = []
    for idx, item in enumerate(items, start=1):
        source_lines.append(
            "\n".join(
                [
                    f"[{idx}] title: {item.title}",
                    f"[{idx}] link: {item.link}",
                    f"[{idx}] published: {item.published}",
                    f"[{idx}] summary: {item.summary}",
                ]
            )
        )
    joined_sources = "\n\n".join(source_lines) if source_lines else "No sources available."

    return (
        f"Topic: {config.topic}\n"
        f"Language: {config.language}\n"
        f"Instruction: {config.instruction}\n\n"
        "Source material:\n"
        f"{joined_sources}\n\n"
        "Return strict JSON only, with this exact schema:\n"
        '{\n'
        '  "title": "string",\n'
        '  "article_markdown": "string in markdown",\n'
        '  "x_post": "string, <=280 chars"\n'
        "}\n"
        "Do not include code fences."
    )


def generate_draft_with_openai(config: AppConfig, items: list[FeedItem]) -> DraftResult:
    api_key = os.getenv(config.openai.api_key_env, "").strip()
    if not api_key:
        raise PipelineError(
            f"Missing OpenAI API key in env var {config.openai.api_key_env}."
        )

    payload = {
        "model": config.openai.model,
        "temperature": 0.4,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful editor. Use only provided source material. "
                    "If information is missing, be explicit about uncertainty."
                ),
            },
            {"role": "user", "content": _build_openai_prompt(config, items)},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    endpoint = f"{config.openai.base_url.rstrip('/')}/chat/completions"
    req = request.Request(
        endpoint,
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=config.request_timeout_seconds) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise PipelineError(f"OpenAI request failed: {exc}") from exc

    try:
        content = response_data["choices"][0]["message"]["content"]
        parsed = _json_from_maybe_markdown(content)
        title = str(parsed["title"]).strip()
        article_markdown = str(parsed["article_markdown"]).strip()
        x_post = str(parsed["x_post"]).strip()
    except (KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        raise PipelineError("OpenAI response format was not valid JSON draft data.") from exc

    if not title or not article_markdown or not x_post:
        raise PipelineError("OpenAI returned empty draft fields.")

    if len(x_post) > 280:
        x_post = x_post[:277] + "..."

    return DraftResult(
        title=title,
        article_markdown=article_markdown,
        x_post=x_post,
        model_used=config.openai.model,
        used_fallback=False,
    )


def generate_fallback_draft(config: AppConfig, items: list[FeedItem]) -> DraftResult:
    date_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"{config.topic} update ({date_label})"
    lines = [
        f"# {title}",
        "",
        "## Highlights",
        "",
    ]
    if items:
        for idx, item in enumerate(items, start=1):
            lines.extend(
                [
                    f"### {idx}. {item.title}",
                    f"- Source: {item.source_feed}",
                    f"- Link: {item.link or 'N/A'}",
                    f"- Published: {item.published or 'N/A'}",
                    f"- Summary: {item.summary or 'N/A'}",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "No source items were collected in this run.",
                "",
                "Action: verify RSS URLs and keyword filters.",
                "",
            ]
        )

    x_line = f"{config.topic}: {len(items)} new item(s) summarized. Full draft is ready."
    if len(x_line) > 280:
        x_line = x_line[:277] + "..."

    return DraftResult(
        title=title,
        article_markdown="\n".join(lines).strip(),
        x_post=x_line,
        model_used="fallback-template",
        used_fallback=True,
    )


def generate_draft(config: AppConfig, items: list[FeedItem]) -> DraftResult:
    try:
        return generate_draft_with_openai(config, items)
    except PipelineError:
        return generate_fallback_draft(config, items)


def compute_content_hash(article_markdown: str, x_post: str) -> str:
    payload = f"{article_markdown}\n---\n{x_post}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PipelineError(f"Required file does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _percent_encode(value: str) -> str:
    return parse.quote(value, safe="~-._")


def build_oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    nonce = nonce or uuid.uuid4().hex
    timestamp = timestamp or str(int(time.time()))

    parsed_url = parse.urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    query_params = parse.parse_qsl(parsed_url.query, keep_blank_values=True)

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": token,
        "oauth_version": "1.0",
    }

    signature_params: list[tuple[str, str]] = []
    signature_params.extend(query_params)
    signature_params.extend((k, v) for k, v in oauth_params.items())
    signature_params.sort(key=lambda kv: (_percent_encode(kv[0]), _percent_encode(kv[1])))

    parameter_string = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in signature_params
    )
    base_elems = [
        method.upper(),
        _percent_encode(base_url),
        _percent_encode(parameter_string),
    ]
    base_string = "&".join(base_elems)
    signing_key = f"{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}"
    signature_raw = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    oauth_signature = base64.b64encode(signature_raw).decode("ascii")
    oauth_params["oauth_signature"] = oauth_signature

    header_params = ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {header_params}"


def load_x_credentials(config: AppConfig) -> XCredentials:
    missing: list[str] = []
    values: dict[str, str] = {}

    env_map = {
        "api_key": config.x.api_key_env,
        "api_secret": config.x.api_secret_env,
        "access_token": config.x.access_token_env,
        "access_token_secret": config.x.access_token_secret_env,
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name, "").strip()
        if not value:
            missing.append(env_name)
        values[key] = value

    if missing:
        joined = ", ".join(missing)
        raise PipelineError(f"Missing X credentials env vars: {joined}")

    return XCredentials(
        api_key=values["api_key"],
        api_secret=values["api_secret"],
        access_token=values["access_token"],
        access_token_secret=values["access_token_secret"],
    )


def publish_to_x(text: str, creds: XCredentials, timeout_seconds: int) -> dict[str, Any]:
    payload = json.dumps({"text": text}).encode("utf-8")
    auth_header = build_oauth1_header(
        method="POST",
        url=X_CREATE_TWEET_URL,
        consumer_key=creds.api_key,
        consumer_secret=creds.api_secret,
        token=creds.access_token,
        token_secret=creds.access_token_secret,
    )
    req = request.Request(
        X_CREATE_TWEET_URL,
        method="POST",
        data=payload,
        headers={
            "Authorization": auth_header,
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            response_json = json.loads(body) if body else {}
            return {
                "ok": True,
                "status_code": resp.status,
                "response": response_json,
            }
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status_code": exc.code,
            "error": error_body,
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status_code": None,
            "error": str(exc),
        }


def _run_paths(base_dir: Path, run_id: str) -> dict[str, Path]:
    run_dir = base_dir / run_id
    return {
        "run_dir": run_dir,
        "sources": run_dir / "sources.json",
        "article": run_dir / "article.md",
        "x_post": run_dir / "x_post.txt",
        "metadata": run_dir / "metadata.json",
        "approval": run_dir / "approval.json",
        "publish_result": run_dir / "publish_result.json",
    }


def create_draft_run(config: AppConfig, run_id: str | None = None) -> str:
    run_id = run_id or create_run_id()
    paths = _run_paths(config.output_dir, run_id)
    run_dir = paths["run_dir"]
    if run_dir.exists():
        raise PipelineError(
            f"Run directory already exists for run_id={run_id}. Choose another run-id."
        )
    run_dir.mkdir(parents=True, exist_ok=False)

    items, source_errors = collect_feed_items(config)
    draft = generate_draft(config, items)

    article_text = draft.article_markdown.strip() + "\n"
    x_post_text = draft.x_post.strip()
    content_hash = compute_content_hash(article_text, x_post_text)

    _write_json(
        paths["sources"],
        {"items": [asdict(item) for item in items], "source_errors": source_errors},
    )
    paths["article"].write_text(article_text, encoding="utf-8")
    paths["x_post"].write_text(x_post_text + "\n", encoding="utf-8")
    _write_json(
        paths["metadata"],
        {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "topic": config.topic,
            "model_used": draft.model_used,
            "used_fallback": draft.used_fallback,
            "item_count": len(items),
            "content_hash": content_hash,
            "config": {
                "max_items": config.max_items,
                "keywords": config.keywords,
                "rss_urls": config.rss_urls,
            },
        },
    )
    return run_id


def approve_run(config: AppConfig, run_id: str) -> None:
    paths = _run_paths(config.output_dir, run_id)
    article_text = paths["article"].read_text(encoding="utf-8")
    x_post_text = paths["x_post"].read_text(encoding="utf-8").strip()
    content_hash = compute_content_hash(article_text, x_post_text)

    _write_json(
        paths["approval"],
        {
            "run_id": run_id,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": content_hash,
        },
    )


def publish_run(config: AppConfig, run_id: str, dry_run: bool = False) -> dict[str, Any]:
    paths = _run_paths(config.output_dir, run_id)
    if not paths["approval"].exists():
        raise PipelineError(
            "Run is not approved yet. Execute `approve` before `publish`."
        )

    approval = _load_json(paths["approval"])
    article_text = paths["article"].read_text(encoding="utf-8")
    x_post_text = paths["x_post"].read_text(encoding="utf-8").strip()
    current_hash = compute_content_hash(article_text, x_post_text)
    approved_hash = str(approval.get("content_hash", ""))
    if current_hash != approved_hash:
        raise PipelineError(
            "Draft changed after approval. Run `approve` again before publishing."
        )

    if dry_run:
        result = {
            "ok": True,
            "status_code": None,
            "response": {"dry_run": True, "text": x_post_text},
        }
    else:
        creds = load_x_credentials(config)
        result = publish_to_x(
            text=x_post_text,
            creds=creds,
            timeout_seconds=config.request_timeout_seconds,
        )

    _write_json(
        paths["publish_result"],
        {
            "run_id": run_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        },
    )
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x_publisher",
        description=(
            "MVP pipeline for RSS collection, draft generation, human approval, and X posting."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_arguments(
        p: argparse.ArgumentParser, run_id_required: bool = False
    ) -> None:
        p.add_argument(
            "--config",
            default=DEFAULT_CONFIG_PATH,
            help=f"Path to config JSON file (default: {DEFAULT_CONFIG_PATH})",
        )
        p.add_argument(
            "--run-id",
            required=run_id_required,
            default=None,
            help="Run id, e.g. 20260101-100000",
        )

    draft_p = subparsers.add_parser("draft", help="Collect sources and generate draft.")
    add_common_arguments(draft_p)

    approve_p = subparsers.add_parser(
        "approve", help="Approve a generated draft before publishing."
    )
    add_common_arguments(approve_p, run_id_required=True)

    publish_p = subparsers.add_parser("publish", help="Publish approved draft to X.")
    add_common_arguments(publish_p, run_id_required=True)
    publish_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call X API; only write publish_result.json",
    )

    run_p = subparsers.add_parser(
        "run",
        help="Convenience command: draft, optional approve, optional publish.",
    )
    add_common_arguments(run_p)
    run_p.add_argument("--auto-approve", action="store_true")
    run_p.add_argument("--auto-publish", action="store_true")
    run_p.add_argument("--dry-run", action="store_true")

    return parser


def _print_paths(config: AppConfig, run_id: str) -> None:
    paths = _run_paths(config.output_dir, run_id)
    print(f"run_id: {run_id}")
    print(f"run_dir: {paths['run_dir']}")
    print(f"article: {paths['article']}")
    print(f"x_post: {paths['x_post']}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)

        if args.command == "draft":
            run_id = create_draft_run(config, run_id=args.run_id)
            _print_paths(config, run_id)
            return 0

        if args.command == "approve":
            approve_run(config, run_id=args.run_id)
            _print_paths(config, args.run_id)
            print("approval: created")
            return 0

        if args.command == "publish":
            result = publish_run(config, run_id=args.run_id, dry_run=args.dry_run)
            _print_paths(config, args.run_id)
            print(f"publish_result_ok: {result.get('ok')}")
            if result.get("response"):
                print("response:", json.dumps(result["response"], ensure_ascii=False))
            if result.get("error"):
                print("error:", result["error"])
            return 0 if result.get("ok") else 2

        if args.command == "run":
            run_id = create_draft_run(config, run_id=args.run_id)
            if args.auto_publish:
                args.auto_approve = True
            if args.auto_approve:
                approve_run(config, run_id=run_id)
            result: dict[str, Any] | None = None
            if args.auto_publish:
                result = publish_run(config, run_id=run_id, dry_run=args.dry_run)
            _print_paths(config, run_id)
            if result is not None:
                print(f"publish_result_ok: {result.get('ok')}")
                return 0 if result.get("ok") else 2
            return 0

        parser.print_help()
        return 1
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


"""Webhook notifications when a run finishes: a Slack incoming-webhook message, or the run summary
as JSON for anything else. A notification that can't be delivered is a warning, never a failed run,
and webhook URLs (which are secrets) are never printed: only their host."""

from __future__ import annotations

import datetime
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .events import fmt_rows, fmt_seconds

SUCCESS, FAILURE, VIEW_ERRORS, TEST = "success", "failure", "view_errors", "test"
_EMOJI = {SUCCESS: "✅", FAILURE: "❌", VIEW_ERRORS: "⚠️", TEST: "🧪"}
_LABEL = {SUCCESS: "replication completed", FAILURE: "replication FAILED",
          VIEW_ERRORS: "replication completed with view errors", TEST: "test notification"}
MAX_LISTED = 10          # failures listed in a Slack message (all of them go into the JSON payload, clipped)
MAX_ERROR_CHARS = 500


def outcome_of(exit_code: int) -> str:
    return SUCCESS if exit_code == 0 else VIEW_ERRORS if exit_code == 3 else FAILURE


def clip(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """Shorten keeping the head and the tail: an error's cause is usually at the end."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return f"{text[:half]} … {text[-half:]}"


def url_host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or "webhook"


def build_payload(outcome: str, exit_code: int, run_id: str | None, replication: str | None, source: str,
                  target: str, catalog: str | None, started_at: datetime.datetime, seconds: float, stats: dict,
                  table_failures: dict[str, str], view_failures: dict[str, str], error: str | None,
                  include_errors: bool = True) -> dict:
    def failures(items: dict[str, str]) -> dict[str, str | None]:
        return {name: (clip(text=err) if include_errors else None) for name, err in items.items()}

    return {
        "event": "adbc_replicate.run_finished" if outcome != TEST else "adbc_replicate.test",
        "outcome": outcome,
        "exit_code": exit_code,
        "replication": replication,
        "run_id": run_id,
        "source": source,
        "target": target,
        "catalog": catalog,
        "started_at": started_at.isoformat(timespec="seconds"),
        "duration_seconds": round(seconds, 1),
        "stats": stats,
        "table_failures": failures(items=table_failures),
        "view_failures": failures(items=view_failures),
        "error": clip(text=error, limit=2000) if (error and include_errors) else (None if not error else "(hidden)"),
        "host": socket.gethostname(),
        "tool_version": __version__,
    }


def slack_message(payload: dict) -> dict:
    """Slack Block Kit message (within Slack's limits: header 150 chars, section text 3,000)."""
    outcome = payload["outcome"]
    title = f"{_EMOJI[outcome]} {payload['replication'] or 'adbc-replicate'}: {_LABEL[outcome]}"[:150]
    stats = payload["stats"]
    fields = [f"*Source → target*\n{payload['source']} → {payload['target']}"
              + (f" ({payload['catalog']})" if payload["catalog"] else "")]
    if "tables_total" in stats:
        fields.append(f"*Tables*\n{stats.get('tables_copied', 0)} copied, {stats.get('tables_skipped', 0)} unchanged, "
                      f"{stats.get('tables_failed', 0)} failed")
        fields.append(f"*Rows copied*\n{fmt_rows(stats.get('rows_copied', 0))}")
    if stats.get("views_total"):
        fields.append(f"*Views*\n{stats.get('views_created', 0)} created, {stats.get('views_skipped', 0)} unchanged, "
                      f"{stats.get('views_failed', 0)} failed")
    fields.append(f"*Duration*\n{fmt_seconds(payload['duration_seconds'])}")
    if stats.get("snapshot"):
        fields.append(f"*Consistent read*\n{stats['snapshot']}")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
              {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]]}]
    if payload["error"]:
        text = f"*Error*\n```{payload['error']}```"[:3000]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    for kind, items in (("Table failures", payload["table_failures"]), ("View failures", payload["view_failures"])):
        if items:
            listed = list(items.items())[:MAX_LISTED]
            lines = [f"• `{name}`" + (f": {err[:300]}" if err else "") for name, err in listed]
            if len(items) > MAX_LISTED:
                lines.append(f"…and {len(items) - MAX_LISTED} more (see the run log)")
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": f"*{kind} ({len(items)})*\n" + "\n".join(lines)}})
            blocks[-1]["text"]["text"] = blocks[-1]["text"]["text"][:3000]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"run `{payload['run_id']}` · host `{payload['host']}` · adbc-replicate {payload['tool_version']}")}]})
    return {"text": title, "blocks": blocks}


def send(url: str, body: dict, timeout: float) -> None:
    """POST JSON; one retry after a connection error or a 5xx. Raises with a message that omits the URL."""
    data = json.dumps(body, default=str).encode("utf-8")
    for attempt in (1, 2):
        request = urllib.request.Request(url=url, data=data, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": f"adbc-replicate/{__version__}"})
        try:
            with urllib.request.urlopen(url=request, timeout=timeout) as response:
                response.read()
            return
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace") if e.fp else ""
            if e.code < 500 or attempt == 2:
                raise RuntimeError(f"HTTP {e.code} from {url_host(url=url)}: {detail.strip()}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 2:
                reason = getattr(e, "reason", e)
                raise RuntimeError(f"can't reach {url_host(url=url)}: {reason}") from None
        time.sleep(2)


def deliver(notifications: list, payload: dict, emit) -> None:
    """Send the payload to every notification whose `when` includes its outcome (a test goes to all)."""
    for notification in notifications:
        if payload["outcome"] != TEST and payload["outcome"] not in notification.when:
            continue
        host = url_host(url=notification.url)
        body = payload if notification.format == "json" else slack_message(payload=payload)
        try:
            send(url=notification.url, body=body, timeout=notification.timeout_seconds)
            emit("notification_sent", f"📣 Notified {host} ({payload['outcome']})", host=host,
                 outcome=payload["outcome"])
        except Exception as e:
            emit("notification_failed", f"⚠️  Notification to {host} failed: {e}", level="warning", host=host,
                 error=str(e))

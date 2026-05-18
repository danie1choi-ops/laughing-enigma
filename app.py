import logging
import os
import re
import ssl
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import certifi
from dotenv import load_dotenv
from google import genai
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.web import WebClient

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not SLACK_BOT_TOKEN:
    raise RuntimeError("Missing SLACK_BOT_TOKEN")
if not SLACK_APP_TOKEN:
    raise RuntimeError("Missing SLACK_APP_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY")


def create_verified_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


slack_ssl_context = create_verified_ssl_context()
slack_client = WebClient(token=SLACK_BOT_TOKEN, ssl=slack_ssl_context)
app = App(client=slack_client)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
logger = logging.getLogger(__name__)

RAW_OUTPUT_LIMIT = 12_000
COMMAND_TIMEOUT_SECONDS = 180
GEMINI_MODEL = "gemini-2.5-flash"
SUMMARY_UNAVAILABLE_PREFIX = "Gemini summary unavailable; showing raw output."
BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
ENERGY_PROJECT_DIR = Path.home() / "Coding" / "energy-arbitrage"
ENERGY_LOG_PATH = LOGS_DIR / "energy_observation.log"
ENERGY_OBSERVE_COMMAND = ["python3", "main.py", "--mode", "live-observe"]
ENERGY_STOP_TIMEOUT_SECONDS = 15
ENERGY_LOCK = threading.Lock()
ENERGY_PROCESS: subprocess.Popen[str] | None = None


@dataclass(frozen=True)
class CommandSpec:
    label: str
    cwd: str
    args: list[str]
    post_args: list[list[str]] | None = None


COMMANDS = {
    "stocks": CommandSpec(
        label="stocks",
        cwd="/Users/danielchoi/Coding/v1",
        args=["python3", "generate_signals.py"],
    ),
    "crypto": CommandSpec(
        label="crypto",
        cwd="/Users/danielchoi/Coding/crypto",
        args=["python3", "main.py", "--mode", "signals"],
        post_args=[
            ["cat", "outputs/signal_summary.csv"],
            ["cat", "outputs/current_portfolio_candidates.csv"],
        ],
    ),
}

ENERGY_COMMANDS = {
    "energy start",
    "energy stop",
    "energy status",
    "energy summary",
    "energy report",
}

HELP_TEXT = (
    "Send `stocks` or `crypto` to run a whitelisted signal command.\n"
    "Energy observer commands: `energy start`, `energy stop`, `energy status`, "
    "`energy summary`, `energy report`.\n"
    "Send `help` to see this message."
)


def run_single_command(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        timeout=COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )


def run_whitelisted_command(name: str) -> str:
    spec = COMMANDS[name]
    outputs = []

    try:
        commands_to_run = [spec.args] + (spec.post_args or [])
        for args in commands_to_run:
            result = run_single_command(args, spec.cwd)
            outputs.append(format_result(args, result))
            if result.returncode != 0:
                break
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        outputs.append(
            "Command timed out\n"
            f"Command: {' '.join(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)])}\n"
            f"Timeout seconds: {COMMAND_TIMEOUT_SECONDS}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )
    except FileNotFoundError as exc:
        outputs.append(f"Command failed before execution\nLikely cause: missing command or directory.\nError: {exc}")

    raw_output = "\n\n".join(outputs)
    return raw_output[-RAW_OUTPUT_LIMIT:]


def format_result(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"Command: {' '.join(args)}\n"
        f"Return code: {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def raw_output_fallback(raw_output: str) -> str:
    return f"{SUMMARY_UNAVAILABLE_PREFIX}\n\n{raw_output}"


def trim_for_slack(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n...truncated..."


def summarize_command_output(command_name: str, raw_output: str) -> str:
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=(
                "You summarize trading signal script output for Slack. "
                "Use exactly this output format:\n\n"
                "Status:\n"
                "Action:\n"
                "Notes:\n\n"
                "Keep it concise. If any return code is non-zero or output indicates failure, "
                "clearly say Command failed and include the likely cause. "
                "Do not invent trades, signals, buys, sells, or actions. "
                "Only report actions that are explicitly present in the raw output.\n\n"
                f"Command: {command_name}\n\nRaw command output:\n{raw_output}"
            ),
        )
        return response.text.strip()
    except Exception:
        logger.exception("Gemini summary failed")
        return raw_output_fallback(raw_output)


def ensure_logs_dir() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def refresh_energy_process() -> subprocess.Popen[str] | None:
    global ENERGY_PROCESS

    if ENERGY_PROCESS is not None and ENERGY_PROCESS.poll() is not None:
        ENERGY_PROCESS = None
    return ENERGY_PROCESS


def is_energy_running() -> bool:
    return refresh_energy_process() is not None


def read_last_log_lines(line_count: int) -> str:
    if not ENERGY_LOG_PATH.exists():
        return ""

    with ENERGY_LOG_PATH.open("r", encoding="utf-8", errors="replace") as log_file:
        return "".join(deque(log_file, maxlen=line_count)).strip()


def format_energy_status() -> str:
    status = "running" if is_energy_running() else "stopped"
    log_tail = read_last_log_lines(20)

    if not log_tail:
        return f"Energy observer is `{status}`. No log available yet."

    return trim_for_slack(f"Energy observer is `{status}`.\n\nLast 20 log lines:\n```{log_tail}```")


def start_energy_observer() -> str:
    global ENERGY_PROCESS

    with ENERGY_LOCK:
        if is_energy_running():
            return "Energy observer is already running."

        if not ENERGY_PROJECT_DIR.exists():
            return f"Energy observer not started. Project directory missing: `{ENERGY_PROJECT_DIR}`"

        if not (ENERGY_PROJECT_DIR / "main.py").exists():
            return (
                "Energy observer not started. `main.py` was not found in the energy project. "
                "`ENERGY_OBSERVE_COMMAND` is defined near the top of `app.py` for editing."
            )

        ensure_logs_dir()
        log_file = ENERGY_LOG_PATH.open("a", encoding="utf-8")
        log_file.write(f"\n--- energy observer start requested {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.flush()

        try:
            ENERGY_PROCESS = subprocess.Popen(
                ENERGY_OBSERVE_COMMAND,
                cwd=ENERGY_PROJECT_DIR,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            log_file.write(f"Failed to start energy observer: {exc}\n")
            return f"Energy observer failed to start: `{exc}`"
        finally:
            log_file.close()

        return f"Energy observer started in observation mode. Log: `{ENERGY_LOG_PATH}`"


def stop_energy_observer() -> str:
    global ENERGY_PROCESS

    with ENERGY_LOCK:
        process = refresh_energy_process()
        if process is None:
            return "Energy observer is not running."

        process.terminate()
        try:
            process.wait(timeout=ENERGY_STOP_TIMEOUT_SECONDS)
            result = f"Energy observer stopped safely with exit code `{process.returncode}`."
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            result = "Energy observer did not stop in time, so it was killed."

        ENERGY_PROCESS = None
        return result


def summarize_energy_log(line_count: int, report: bool = False) -> str:
    log_tail = read_last_log_lines(line_count)
    if not log_tail:
        return "No energy observation log is available yet."

    if report:
        prompt = (
            "You are diagnosing an energy arbitrage live observation log for Slack. "
            "Use exactly this output format:\n\n"
            "Status:\n"
            "Interesting Events:\n"
            "Potential Issues:\n"
            "Recommendation:\n\n"
            "Flag bugs, stale data, strange repeated HOLD behaviour, missing timestamps, "
            "command errors, volatile price behaviour, or anything operationally suspicious. "
            "Do not invent data. Only use what appears in the log. If evidence is absent, say so.\n\n"
            f"Last {line_count} log lines:\n{log_tail}"
        )
    else:
        prompt = (
            "You summarize an energy arbitrage live observation log for Slack. "
            "Use exactly this output format:\n\n"
            "Status:\n"
            "Action:\n"
            "Notes:\n\n"
            "Keep it concise. Do not invent data, prices, timestamps, or actions. "
            "Only summarize what appears in the log.\n\n"
            f"Last {line_count} log lines:\n{log_tail}"
        )

    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return trim_for_slack(response.text.strip(), limit=6000 if report else 3500)
    except Exception:
        logger.exception("Gemini energy summary failed")
        return trim_for_slack(raw_output_fallback(log_tail))


def handle_energy_command(text: str) -> str:
    if text == "energy start":
        return start_energy_observer()
    if text == "energy stop":
        return stop_energy_observer()
    if text == "energy status":
        return format_energy_status()
    if text == "energy summary":
        return summarize_energy_log(200)
    if text == "energy report":
        return summarize_energy_log(500, report=True)
    return "Unknown energy command. Send `help` for supported commands."


def starts_with_bot_mention(text: str, bot_user_id: str | None) -> bool:
    if not bot_user_id:
        return False
    return bool(re.match(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", text))


def normalize_slack_command(text: str, bot_user_id: str | None = None) -> str:
    normalized = text.strip()
    if bot_user_id:
        normalized = re.sub(
            rf"^<@{re.escape(bot_user_id)}(?:\|[^>]+)?>\s*",
            "",
            normalized,
            count=1,
        )
    return normalized.strip().lower()


def log_slack_message_event(event: dict, command: str) -> None:
    print(
        "Slack message event received: "
        f"channel={event.get('channel')} "
        f"user={event.get('user')} "
        f"normalised_command={command!r}"
    )


def dispatch_command(command: str, say, logger) -> None:
    if command == "help":
        say(HELP_TEXT)
        return

    if command in ENERGY_COMMANDS:
        say(handle_energy_command(command))
        return

    if command not in COMMANDS:
        return

    say(f"Running `{command}`...")
    raw_output = run_whitelisted_command(command)
    try:
        summary = summarize_command_output(command, raw_output)
    except Exception:
        logger.exception("Gemini summary failed")
        summary = raw_output_fallback(raw_output)
    say(summary)


@app.event("message")
def handle_message_events(body, say, logger, context):
    event = body.get("event", {})
    bot_user_id = context.get("bot_user_id")

    if event.get("bot_id") or event.get("subtype") or event.get("user") == bot_user_id:
        return

    channel_type = event.get("channel_type")
    raw_text = event.get("text") or ""
    command = normalize_slack_command(raw_text, bot_user_id)
    log_slack_message_event(event, command)

    if channel_type not in {"im", "channel"}:
        return

    if channel_type != "im" and starts_with_bot_mention(raw_text, bot_user_id):
        return

    dispatch_command(command, say, logger)


@app.event("app_mention")
def handle_app_mention_events(body, say, logger, context):
    event = body.get("event", {})
    bot_user_id = context.get("bot_user_id")

    if event.get("bot_id") or event.get("subtype") or event.get("user") == bot_user_id:
        return

    command = normalize_slack_command(event.get("text") or "", bot_user_id)
    log_slack_message_event(event, command)
    dispatch_command(command, say, logger)


if __name__ == "__main__":
    print(f"Using certifi CA bundle: {certifi.where()}")
    SocketModeHandler(app, SLACK_APP_TOKEN, web_client=slack_client).start()

import logging
import os
import ssl
import subprocess
from dataclasses import dataclass

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

HELP_TEXT = "Send `stocks` or `crypto` to run a whitelisted signal command. Send `help` to see this message."


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


@app.event("message")
def handle_message_events(body, say, logger):
    event = body.get("event", {})

    if event.get("bot_id") or event.get("subtype"):
        return

    channel_type = event.get("channel_type")
    text = (event.get("text") or "").strip().lower()

    if channel_type != "im":
        return

    if text == "help":
        say(HELP_TEXT)
        return

    if text not in COMMANDS:
        say("Unknown command. Send `help`, `stocks`, or `crypto`.")
        return

    say(f"Running `{text}`...")
    raw_output = run_whitelisted_command(text)
    try:
        summary = summarize_command_output(text, raw_output)
    except Exception as exc:
        logger.exception("Gemini summary failed")
        summary = raw_output_fallback(raw_output)
    say(summary)


if __name__ == "__main__":
    print(f"Using certifi CA bundle: {certifi.where()}")
    SocketModeHandler(app, SLACK_APP_TOKEN, web_client=slack_client).start()

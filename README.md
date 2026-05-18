# AI Slack Agent

A local Slack Socket Mode bot for running whitelisted trading signal scripts from Slack DMs and returning concise Gemini summaries.

## Commands

Send one of these exact messages in a DM to the bot:

- `help`
- `stocks`
- `crypto`

The bot does not accept arbitrary shell commands. It only runs the whitelisted commands configured in `app.py`.

## Setup

### 1. Create a virtual environment

```bash
cd ~/Coding/ai-slack-agent
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install packages

```bash
pip install --upgrade -r requirements.txt
```

Or install the runtime packages directly:

```bash
pip install --upgrade google-genai python-dotenv slack_bolt slack_sdk certifi
```

### 3. Create a Slack app

1. Go to <https://api.slack.com/apps>.
2. Click **Create New App**.
3. Choose **From scratch**.
4. Pick a workspace where you can install the app.

### 4. Enable Socket Mode

1. In the Slack app settings, open **Socket Mode**.
2. Enable Socket Mode.
3. Create an app-level token with the `connections:write` scope.
4. Copy the token that starts with `xapp-`.

### 5. Add required Slack scopes

In **OAuth & Permissions**, add these Bot Token Scopes:

- `chat:write`
- `im:history`
- `im:read`
- `im:write`

Then install or reinstall the app to your workspace and copy the bot token that starts with `xoxb-`.

### 6. Fill `.env`

```bash
cp .env.example .env
```

Create a Gemini API key in Google AI Studio at <https://aistudio.google.com/app/apikey>.

Edit `.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_APP_TOKEN=xapp-your-slack-app-level-token
GEMINI_API_KEY=your-gemini-api-key
```

### 7. Run the bot

```bash
python3 app.py
```

On startup, the bot prints the `certifi` CA bundle path it will use for Slack HTTPS and Socket Mode TLS verification. This is safe to share because it does not include any Slack or Gemini secrets.

DM the bot `help`, `stocks`, or `crypto`.

## SSL certificate troubleshooting

If Python raises this SSL error on macOS:

```text
urllib.error.URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate>
```

install or refresh `certifi` and the bot dependencies inside the project virtual environment:

```bash
cd ~/Coding/ai-slack-agent
source .venv/bin/activate
pip install --upgrade google-genai python-dotenv slack_bolt slack_sdk certifi
python3 app.py
```

The app keeps SSL certificate verification enabled and configures Slack SDK to use the `certifi` CA bundle.

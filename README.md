# AIPhoneTools

An AI Agent server running in Android Termux environment, enabling AI to control your phone — read/write files, execute commands, and call Termux-API.

## Features

- **Multi-Model Support** - Dynamically load model lists, switch freely in Chatbox
- **Local Tool Execution** - 7 tools: file read/write, command execution, directory listing, file search, system status, global memory update
- **Termux-API Integration** - Support 40+ features: GPS, clipboard, notifications, TTS, camera, etc.
- **Long-Term Memory** - Auto-load `~/memory.md` (up to 8000 chars) on the first message of each new conversation
- **Multi-Round Tool Calling** - Up to 20 rounds, auto-batched execution with real-time progress display
- **Time Budget Mechanism** - 50s auto-interrupt to prevent client timeout
- **SSE Heartbeat** - Space-char heartbeat every 5s during tool execution to prevent SSE idle timeout
- **OpenAI Compatible** - Standard API interface, works with any OpenAI client

## Quick Start

### 1. Install Termux

Download from [Google Play](https://play.google.com/store/apps/details?id=com.termux), [F-Droid](https://f-droid.org/packages/com.termux/), or [GitHub Releases](https://github.com/termux/termux-app/releases).

> **Note**: If you plan to use Termux-API features (GPS, camera, SMS, etc.), you must also install the [Termux:API app from F-Droid](https://f-droid.org/packages/com.termux.api/) — it is not available on Google Play.

### 2. Install Dependencies

```bash
# Update packages
pkg update && pkg upgrade -y

# Install Python and Git
pkg install python git -y

# Grant storage permission
termux-setup-storage

# Clone repository
git clone https://github.com/RayDutchman/AIPhoneTools.git
cd AIPhoneTools

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Configure API

Copy example config and fill in your API Key:

```bash
cp models_config.example.json models_config.json
nano models_config.json  # or use other editor
```

**Config Example:**

```json
{
  "providers": {
    "openai": {
      "name": "OpenAI",
      "api_base": "https://api.openai.com",
      "api_key": "sk-your-key-here",
      "models": [
        {
          "id": "claude-sonnet-4-6",
          "name": "Claude Sonnet 4.6",
          "supports_tools": true,
          "max_tokens": 8192
        }
      ]
    }
  },
  "default_provider": "openai",
  "default_model": "claude-sonnet-4-6"
}
```

### 4. Start Server

```bash
# Foreground (for testing)
python server.py

# Background (production)
nohup python server.py > ~/server.log 2>&1 &

# View logs
tail -f ~/server.log
```

### 5. Configure Chatbox

1. Download [Chatbox](https://chatboxai.app/)
2. Settings → AI Provider → Add Custom API
3. Configure:
   - **API URL**: `http://phone-ip:5846` (use `hostname -I` to check IP)
   - **API Key**: any value

> **Tip**: In Chatbox conversation settings, limit "Context Messages" to around 10 to prevent excessively long conversations from causing upstream token limit errors.

## Available Tools

| Tool | Function | Example |
|------|----------|---------|
| `read_phone_file` | Read file | "Read memory.md" |
| `write_phone_file` | Write file | "Create notes.txt with content..." |
| `execute_local_command` | Execute command | "Run ls -la" |
| `list_phone_dir` | List directory | "List current directory" |
| `search_phone_files` | Search files | "Search all .py files" |
| `get_phone_system_status` | System status | "Get battery and memory" |
| `update_global_memory` | Update memory.md | "Remember that I prefer Python" |

### Termux-API Support

Call 40+ Termux-API commands via `execute_local_command`:

```bash
# Install the termux-api package inside Termux
pkg install termux-api -y
```

> **Required**: Also install the [Termux:API app from F-Droid](https://f-droid.org/packages/com.termux.api/) — the package above alone is not enough.

**Common Commands:**

- `termux-location` - GPS location
- `termux-clipboard-get/set` - Clipboard
- `termux-notification` - Notifications
- `termux-tts-speak` - Text-to-speech
- `termux-camera-photo` - Take photo
- `termux-sms-list/send` - SMS
- `termux-toast` - Toast message
- `termux-vibrate` - Vibrate
- `termux-torch` - Flashlight
- `termux-wifi-connectioninfo` - WiFi info

Full list: `termux-api --help`

## Model Management

Use `update_models.py` to manage multiple Providers and models:

```bash
# Interactive menu
python update_models.py

# Command-line usage
python update_models.py list          # List all models
python update_models.py add-provider  # Add Provider
python update_models.py add-model     # Add model
python update_models.py test          # Test connection
python update_models.py set-default   # Set default model
```

## Advanced Usage

### Restart Server

```bash
pkill -f server.py && nohup python server.py > ~/server.log 2>&1 &
```

### Long-Term Memory

Create `~/memory.md` — AI will auto-load it at the start of each new conversation (up to 8000 chars). Use the `update_global_memory` tool to let AI update it during a conversation.

```bash
echo "# My Memory\n\n- I prefer Python\n- My projects are in ~/projects" > ~/memory.md
```

### Auto-Start Script

Create `~/start_server.sh`:

```bash
#!/data/data/com.termux/files/usr/bin/bash
cd ~/AIPhoneTools
nohup python server.py > ~/server.log 2>&1 &
echo "Server started. Log: tail -f ~/server.log"
```

```bash
chmod +x ~/start_server.sh
~/start_server.sh
```

### SSH Remote Management

```bash
# Install and start SSH
pkg install openssh -y
sshd

# Connect from PC (default port 8022)
ssh -p 8022 u0_aXXX@phone-ip
```

## Troubleshooting

### Chatbox Connection Failed

```bash
# Check if server is running
ps aux | grep server.py

# Confirm IP address
hostname -I

# Test locally
curl http://localhost:5846/v1/models
```

### Tool Execution Failed

Check `[TOOL]` info in logs:

```bash
tail -50 ~/server.log | grep TOOL
```

### AI Repeats Tool Calls or Gets 502 Error

This is usually caused by an overly long conversation history exceeding the upstream provider's token limit. Fix: in Chatbox conversation settings, set "Context Messages" to 10 or fewer.

### View Logs

```bash
# All logs
tail -f ~/server.log

# Tool execution only
tail -f ~/server.log | grep TOOL

# Errors and warnings only
tail -f ~/server.log | grep -E "ERROR|WARNING"
```

## API Endpoints

### POST /v1/chat/completions

OpenAI-compatible chat interface. The server is fully stateless — Chatbox owns the conversation history.

### GET /v1/models

List all available models from `models_config.json`.

## Configuration

In `server.py` header:

```python
DOWNLOAD_DIR = os.path.expanduser("~")  # Working directory
TOOL_OUTPUT_MAX_CHARS = 8000            # Tool output limit
```

## Project Structure

```
AIPhoneTools/
├── server.py                     # Main server
├── update_models.py              # Model management tool
├── models_config.json            # API config (not committed)
├── models_config.example.json    # Config template
├── requirements.txt              # Python dependencies
├── .gitignore                    # Git ignore rules
├── README.md                     # This document (English)
└── README.zh-CN.md               # Chinese documentation
```

## Notes

- **Battery**: Enable "Run in background" for Termux in system settings
- **Security**: Don't commit `models_config.json` to public repositories

## Changelog

### v2.1.0 (2026-05-28)

**Improvements:**
- Memory injection now only happens on the first message of a new conversation, avoiding redundant token usage in ongoing conversations
- Raised memory.md read limit from 2000 to 8000 characters
- Added `update_global_memory` tool length constraint (max 8000 chars total)

### v2.0.0 (2026-05-28)

**Features:**
- Stateless design: Chatbox owns conversation history, no server-side session state
- Multi-round tool calling loop (up to 20 rounds)
- Time budget mechanism (50s auto-interrupt)
- SSE heartbeat during tool execution (prevents idle timeout)
- Auto-load memory.md into system prompt
- Preserve Chatbox system messages
- Termux-API command integration

### v1.0.0 (2026-05-26)

**Initial Release:**
- 6 local tools
- OpenAI-compatible interface

## License

MIT License

## Links

- [Termux Official](https://termux.dev/)
- [Chatbox Official](https://chatboxai.app/)
- [Project GitHub](https://github.com/RayDutchman/AIPhoneTools)
- [中文文档](README.zh-CN.md)

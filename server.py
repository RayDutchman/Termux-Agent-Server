import os
import glob
import json
import time
import shutil
import logging
import subprocess
import requests
from flask import Flask, request, jsonify, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ==== 1. Basic Configuration ====

DOWNLOAD_DIR = os.path.expanduser("~")

# Max chars for tool output, truncate if exceeded
TOOL_OUTPUT_MAX_CHARS = 8000
# Global memory file (shared across all sessions)
GLOBAL_MEMORY_PATH = os.path.join(DOWNLOAD_DIR, "memory.md")

# ==== 1b. Multi-model config loading ====
# Load models_config.json on startup; fall back to an empty template if missing.

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_config.json")

# Empty template — no hardcoded provider or key.
# If models_config.json is missing, the startup prompt will force the user
# to enter a URL and API key before the server starts.
_FALLBACK_CONFIG = {
    "providers": {},
    "default_provider": "",
    "default_model": ""
}


def _load_models_config() -> dict:
    """Load models_config.json, fall back to an empty template on failure."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        log.info(f"[CONFIG] Loaded models_config.json, providers={list(cfg.get('providers', {}).keys())}")
        return cfg
    except FileNotFoundError:
        log.warning("[CONFIG] models_config.json not found, using fallback config")
        return _FALLBACK_CONFIG
    except Exception as e:
        log.error(f"[CONFIG] Failed to load models_config.json: {e}, using fallback config")
        return _FALLBACK_CONFIG


# Load once at startup; restart service after update_models.py modifies config
MODELS_CONFIG: dict = _load_models_config()


def get_provider_for_model(model_id: str):
    """
    Look up the provider and model config for a given model_id.
    Returns (provider_dict, model_dict).

    Fallback priority:
    1. Exact match in any provider's models list.
    2. If not found but only one provider exists, use that provider and
       pass model_id as-is to the upstream API (transparent proxy).
    3. If multiple providers exist, use the default provider and pass
       model_id as-is (still transparent — don't swap the model ID).
    """
    model_id = model_id.strip()  # Defensively strip leading/trailing spaces
    for provider in MODELS_CONFIG.get("providers", {}).values():
        for model in provider.get("models", []):
            if model["id"] == model_id:
                return provider, model

    # model_id not in our fetched list — build a synthetic model entry
    # so the upstream receives exactly the model_id the client requested.
    synthetic_model = {"id": model_id, "supports_tools": True, "max_tokens": 8192}

    providers = MODELS_CONFIG.get("providers", {})
    if len(providers) == 1:
        # Single provider: always use it, pass model_id through
        provider = next(iter(providers.values()))
        log.warning(f"[CONFIG] Model {model_id!r} not in fetched list, passing through to {provider.get('name')}")
        return provider, synthetic_model

    # Multiple providers: use default provider, still pass model_id through
    default_provider_id = MODELS_CONFIG.get("default_provider", "")
    default_provider = providers.get(default_provider_id, next(iter(providers.values()), {}))
    log.warning(f"[CONFIG] Model {model_id!r} not in fetched list, passing through to default provider {default_provider.get('name')!r}")
    if not default_provider.get("api_key", "").strip():
        log.error("[CONFIG] Default provider has no API key — requests will fail")
    return default_provider, synthetic_model


def get_default_model_id() -> str:
    return MODELS_CONFIG.get("default_model", "claude-sonnet-4-6")


# ==== 2. Tool Functions ====

def read_phone_file(filename):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        size = os.path.getsize(path)
        if size > 500 * 1024:
            return f"Error: File {filename} too large ({size // 1024}KB), please specify a smaller file"
        # Try UTF-8 first, fallback to latin-1 (guaranteed not to throw)
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        return f"Error: Cannot read file {filename} with any known encoding"
    except FileNotFoundError:
        return f"Error: File not found: {filename}"
    except Exception as e:
        return f"Error: Cannot read file {filename}. Reason: {str(e)}"


def write_phone_file(filename, content):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Success: Saved to {path}"
    except Exception as e:
        return f"Error: Cannot write file. Reason: {str(e)}"


def execute_local_command(command=None, **kwargs):
    if command is None:
        command = kwargs.get("cmd") or kwargs.get("shell_command") or kwargs.get("shell") or ""
    if not command:
        return "Error: No command provided"
    try:
        result = subprocess.run(
            command, shell=True, text=True, capture_output=True, timeout=30
        )
        output = f"[Exit Code]: {result.returncode}\n[Stdout]:\n{result.stdout}\n[Stderr]:\n{result.stderr}"
        if len(output) > TOOL_OUTPUT_MAX_CHARS:
            output = output[:TOOL_OUTPUT_MAX_CHARS] + f"\n...[Output too long, truncated to {TOOL_OUTPUT_MAX_CHARS} chars]"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Command execution timeout (30s)"
    except Exception as e:
        return f"Error: Command execution failed. Reason: {str(e)}"


def list_phone_dir(sub_dir=""):
    target_dir = os.path.join(DOWNLOAD_DIR, sub_dir) if sub_dir else DOWNLOAD_DIR
    try:
        if not os.path.exists(target_dir):
            return f"Error: Directory not found: {target_dir}"
        items = os.listdir(target_dir)
        return json.dumps(
            {"directory": target_dir, "contents": items, "count": len(items)},
            ensure_ascii=False, indent=2
        )
    except Exception as e:
        return f"Error: Cannot list directory. Reason: {str(e)}"


def search_phone_files(query_pattern):
    try:
        search_path = os.path.join(DOWNLOAD_DIR, "**", query_pattern)
        results = glob.glob(search_path, recursive=True)
        relative_results = [os.path.relpath(p, DOWNLOAD_DIR) for p in results]
        return json.dumps(
            {"found_files": relative_results, "count": len(relative_results)},
            ensure_ascii=False, indent=2
        )
    except Exception as e:
        return f"Error: Search failed. Reason: {str(e)}"


def get_phone_system_status():
    status = {}
    try:
        total, used, free = shutil.disk_usage(DOWNLOAD_DIR)
        status["storage_free_gb"] = f"{free / (1024**3):.2f} GB"
        status["storage_used_gb"] = f"{used / (1024**3):.2f} GB"

        mem_result = subprocess.run("free -m", shell=True, text=True, capture_output=True)
        status["memory_info_mb"] = mem_result.stdout.strip()

        battery_result = subprocess.run(
            "termux-battery-status", shell=True, text=True, capture_output=True
        )
        if battery_result.returncode == 0 and battery_result.stdout.strip():
            try:
                status["battery"] = json.loads(battery_result.stdout)
            except json.JSONDecodeError:
                status["battery"] = battery_result.stdout.strip()
        else:
            status["battery"] = "Termux-API not installed or cannot get battery info"

        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: Failed to get system status. Reason: {str(e)}"


def update_global_memory(content, mode="append"):
    """
    Update global memory (memory.md) shared across all sessions.
    Use this to save important information that should persist across conversations.
    
    Args:
        content: Content to write
        mode: "append" (add to end), "prepend" (add to start), or "replace" (overwrite)
    """
    try:
        if mode == "replace":
            with open(GLOBAL_MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Success: Global memory replaced with {len(content)} chars"
        
        # Read existing content
        existing = ""
        if os.path.exists(GLOBAL_MEMORY_PATH):
            with open(GLOBAL_MEMORY_PATH, "r", encoding="utf-8") as f:
                existing = f.read()
        
        # Append or prepend
        if mode == "append":
            new_content = existing + "\n\n" + content if existing else content
        elif mode == "prepend":
            new_content = content + "\n\n" + existing if existing else content
        else:
            return f"Error: Invalid mode '{mode}'. Use 'append', 'prepend', or 'replace'"
        
        with open(GLOBAL_MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)
        
        return f"Success: Global memory updated ({mode}), total {len(new_content)} chars"
    except Exception as e:
        return f"Error: Failed to update global memory. Reason: {str(e)}"


tools_map = {
    "read_phone_file": read_phone_file,
    "write_phone_file": write_phone_file,
    "execute_local_command": execute_local_command,
    "list_phone_dir": list_phone_dir,
    "search_phone_files": search_phone_files,
    "get_phone_system_status": get_phone_system_status,
    "update_global_memory": update_global_memory,
}

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "read_phone_file",
            "description": "Read text, CSV or code files from Termux home directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename, e.g. data.csv or subdir/filename"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_phone_file",
            "description": "Write or create file in Termux home directory, supports subdirectories (auto-created)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename to save, e.g. summary.txt or notes/todo.txt"},
                    "content": {"type": "string", "description": "Content to write to file"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_local_command",
            "description": "Execute shell command in Termux environment, e.g. run scripts, pip install, view directories",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute, e.g. ls ~, python script.py"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_phone_dir",
            "description": "List files and folders in Termux home directory or subdirectories",
            "parameters": {
                "type": "object",
                "properties": {
                    "sub_dir": {
                        "type": "string",
                        "description": "Subdirectory name, e.g. 'projects'. Leave empty to list home root"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_phone_files",
            "description": "Search files by wildcard pattern in Termux home directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_pattern": {
                        "type": "string",
                        "description": "Search pattern, e.g. '*.py', '*.pdf', '*invoice*'"
                    }
                },
                "required": ["query_pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_phone_system_status",
            "description": "Get current phone storage, memory usage and battery status",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_global_memory",
            "description": "Update global memory (memory.md) shared across all conversations. Use this to save important information that should persist across sessions, like user preferences, project info, or learned facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Content to add or replace in global memory. Total memory.md must not exceed 8000 characters."},
                    "mode": {
                        "type": "string",
                        "enum": ["append", "prepend", "replace"],
                        "description": "How to update: 'append' (add to end, default), 'prepend' (add to start), 'replace' (overwrite all)"
                    }
                },
                "required": ["content"]
            }
        }
    }
]


# ==== 4. LLM Requests ====

def make_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def safe_get_choice(data):
    """Safely get choices[0].message, return dict or empty dict."""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return {}
    return choices[0].get("message") or {}


def call_llm_sync(messages, tools=None, model_id: str = None):
    """
    Non-streaming request, return parsed dict.
    Use default model when model_id is empty.
    """
    model_id = model_id or get_default_model_id()
    provider, model = get_provider_for_model(model_id)

    api_url = f"{provider['api_base']}/v1/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": False}
    # Only include tools field when model declares tool support
    if tools and model.get("supports_tools", True):
        payload["tools"] = tools

    t0 = time.time()
    log.info(f"[sync] model={model_id!r}, provider={provider.get('name')}, messages={len(messages)}, tools={'yes' if tools else 'no'}")
    try:
        resp = requests.post(
            api_url, json=payload,
            headers=make_headers(provider["api_key"]),
            timeout=120
        )
        resp.raise_for_status()
        result = json.loads(resp.content.decode("utf-8"))
        log.info(f"[sync] Done in {time.time()-t0:.1f}s")
        return result
    except requests.exceptions.Timeout:
        raise RuntimeError("Upstream LLM request timed out")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Upstream LLM error: {e.response.status_code} {e.response.text[:200]}")
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        raise RuntimeError(f"Request/parse failed: {str(e)}")


def call_llm_stream(messages, tools=None, model_id: str = None):
    """
    Streaming request, return requests.Response object (body not read).
    Use default model when model_id is empty.
    """
    model_id = model_id or get_default_model_id()
    provider, model = get_provider_for_model(model_id)

    api_url = f"{provider['api_base']}/v1/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": True}
    if tools and model.get("supports_tools", True):
        payload["tools"] = tools

    log.info(f"[stream] model={model_id!r}, provider={provider.get('name')}, messages={len(messages)}")
    try:
        resp = requests.post(
            api_url, json=payload,
            headers=make_headers(provider["api_key"]),
            stream=True, timeout=(30, 300)
        )
        resp.raise_for_status()
        return resp
    except requests.exceptions.Timeout:
        raise RuntimeError("Upstream LLM request timed out")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Upstream LLM error: {e.response.status_code} {e.response.text[:200]}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network request failed: {str(e)}")


# ==== 5. Tool Execution ====

def execute_all_tool_calls(tool_calls):
    """Execute all tool_calls and return a list of tool result messages."""
    results = []
    for tool_call in tool_calls:
        func_info = tool_call.get("function", {})
        func_name = func_info.get("name", "")
        tool_call_id = tool_call.get("id", "unknown")

        raw_arguments = func_info.get("arguments", "")
        log.info(f"[TOOL_EXEC] {func_name} args={raw_arguments[:200]}")

        # Fix malformed upstream API response: strip leading '{}'
        if raw_arguments.startswith("{}"):
            raw_arguments = raw_arguments[2:]
            log.info(f"[TOOL_EXEC] Stripped leading {{}} from arguments")

        # Empty args use {} directly, skip JSON parsing (avoid meaningless WARNING)
        if not raw_arguments.strip():
            func_args = {}
        else:
            try:
                func_args = json.loads(raw_arguments)
                if not isinstance(func_args, dict):
                    func_args = {}
            except json.JSONDecodeError as e:
                log.warning(f"[TOOL_EXEC] JSON parse failed: {e}, raw={raw_arguments[:100]!r}")
                func_args = {}

        if not func_name:
            result = "Error: empty tool name"
        elif func_name in tools_map:
            try:
                result = tools_map[func_name](**func_args)
            except TypeError as e:
                result = f"Error: argument mismatch ({str(e)}), received: {func_args}"
            except Exception as e:
                result = f"Error: tool execution failed: {str(e)}"
        else:
            result = f"Error: unknown tool '{func_name}'"

        result_str = str(result)
        if len(result_str) > TOOL_OUTPUT_MAX_CHARS:
            result_str = result_str[:TOOL_OUTPUT_MAX_CHARS] + "\n...[truncated]"

        results.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": func_name,
            "content": result_str,
        })
    return results


# ==== 6. SSE Helper Functions ====

def _make_sse_chunk(content=None, finish_reason=None, resp_id="", role=None, created=None, model_id=None):
    """
    Construct an OpenAI-compliant SSE data chunk byte string.
    Spec requires: id, object, created, model, choices
    choices[] requires: index, delta, finish_reason (can be null)
    """
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    chunk = {
        "id": resp_id or f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model_id or get_default_model_id(),
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def make_error_stream(message):
    """Wrap an error message into a valid SSE chunk sequence."""
    err_id = f"err-{int(time.time())}"
    yield _make_sse_chunk(content=f"[error] {message}", resp_id=err_id, role="assistant")
    yield _make_sse_chunk(finish_reason="stop", resp_id=err_id)
    yield b"data: [DONE]\n\n"


# ==== 7. Routes ====

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    chatbox_data = request.json or {}
    want_stream = chatbox_data.get("stream", False)

    # Extract model_id, use default if not specified
    model_id = (chatbox_data.get("model") or get_default_model_id()).strip()

    # Separate system messages from conversation history
    chatbox_system_msgs = [m for m in chatbox_data.get("messages", []) if m.get("role") == "system"]
    incoming = [m for m in chatbox_data.get("messages", []) if m.get("role") != "system"]

    latest_user_msg = next((m for m in reversed(incoming) if m["role"] == "user"), None)
    log.info(f"[REQUEST] model={model_id!r}, stream={want_stream}, messages={len(incoming)}")
    if latest_user_msg:
        log.info(f"[REQUEST] latest_user: {latest_user_msg.get('content', '')[:50]}...")

    # Build system prompt: base instructions + memory.md + Chatbox system message
    system_parts = []
    
    # 1. Base system prompt
    system_parts.append(
        f"You are a versatile AI assistant running inside Termux on an Android phone. "
        f"Your working directory is {DOWNLOAD_DIR}. "
        f"Use the available tools when you need to read/write files or run commands. "
        f"For general conversation, reply directly without calling tools unnecessarily.\n\n"
        f"Available termux-api commands (use via execute_local_command):\n"
        f"- termux-location: get GPS location (JSON)\n"
        f"- termux-clipboard-get/set: read/write clipboard\n"
        f"- termux-notification: send notification to status bar\n"
        f"- termux-tts-speak <text>: text to speech\n"
        f"- termux-camera-photo -c <0|1> <file>: take photo (0=back, 1=front)\n"
        f"- termux-sms-list: list SMS messages (requires permission)\n"
        f"- termux-toast <text>: show toast message\n"
        f"- termux-vibrate: vibrate phone\n"
        f"- termux-torch on|off: toggle flashlight\n"
        f"- termux-wifi-connectioninfo: wifi connection info (JSON)\n"
        f"- termux-battery-status: already used in get_phone_system_status\n"
        f"See 'termux-api --help' for full list."
    )
    
     # 2. Auto-load memory.md (only for new conversations, i.e. first message)
    # For ongoing conversations (history > 1), memory was already injected in the
    # first request and is present in the conversation history - skip to avoid
    # redundant token usage and context bloat.
    is_new_conversation = len(incoming) <= 1
    if is_new_conversation and os.path.exists(GLOBAL_MEMORY_PATH):
        try:
            with open(GLOBAL_MEMORY_PATH, "r", encoding="utf-8") as f:
                memory_content = f.read(8000)  # Limit to 8000 chars
            # Sanity check: skip if content looks like binary/corrupted data
            # (printable chars should make up >80% of valid markdown)
            if memory_content.strip():
                printable = sum(1 for c in memory_content if c.isprintable() or c in "\n\r\t")
                ratio = printable / len(memory_content)
                if ratio < 0.8:
                    log.warning(f"[MEMORY] memory.md looks like binary data (printable ratio={ratio:.2f}), skipping")
                else:
                    system_parts.append(f"\n\n--- Long-term Memory (from ~/memory.md) ---\n{memory_content}")
                    log.info(f"[MEMORY] Loaded {len(memory_content)} chars from memory.md")
        except UnicodeDecodeError:
            log.warning("[MEMORY] memory.md is not valid UTF-8 text, skipping")
        except Exception as e:
            log.warning(f"[MEMORY] Failed to load memory.md: {e}")
    elif not is_new_conversation:
        log.info(f"[MEMORY] Skipped (ongoing conversation, history={len(incoming)} messages)")
    
    # 3. Merge system message from Chatbox (if any)
    if chatbox_system_msgs:
        for msg in chatbox_system_msgs:
            content = msg.get("content", "").strip()
            if content:
                system_parts.append(f"\n\n--- User's Custom Instructions ---\n{content}")
        log.info(f"[SYSTEM] Merged {len(chatbox_system_msgs)} system message(s) from Chatbox")
    
    system_prompt = {
        "role": "system",
        "content": "".join(system_parts)
    }

    # Use history from Chatbox (stateless server design)
    # Chatbox sends complete conversation history in each request
    messages = [system_prompt] + incoming

    log.info(f"[REQUEST] history={len(incoming)} messages")

    # ---- Non-streaming mode: multi-round tool calling loop ----
    if not want_stream:
        MAX_TOOL_ROUNDS = 20
        try:
            current_data = call_llm_sync(messages, tools=tools_schema, model_id=model_id)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 502

        for tool_round in range(MAX_TOOL_ROUNDS):
            choice = safe_get_choice(current_data)
            if not choice.get("tool_calls"):
                break

            tool_calls_list = choice["tool_calls"]
            log.info(f"[TOOL] Round {tool_round + 1}, {len(tool_calls_list)} call(s): {[tc.get('function',{}).get('name') for tc in tool_calls_list]}")
            messages.append(choice)
            tool_results = execute_all_tool_calls(tool_calls_list)
            messages.extend(tool_results)
            log.info(f"[TOOL] Execution done, result lengths: {[len(r['content']) for r in tool_results]}")

            try:
                current_data = call_llm_sync(messages, tools=tools_schema, model_id=model_id)
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 502
        else:
            log.warning(f"[TOOL] Reached max tool rounds ({MAX_TOOL_ROUNDS}) in non-stream mode")

        # Session managed by Chatbox (stateless server)
        return Response(
            json.dumps(current_data, ensure_ascii=False),
            content_type='application/json; charset=utf-8'
        )

    # ---- Streaming mode: receive and forward immediately, respond to Chatbox instantly ----
    def _generate():
        request_start_time = time.time()  # Start time of entire request
        try:
            first_resp = call_llm_stream(messages, tools=tools_schema, model_id=model_id)
        except RuntimeError as e:
            yield from make_error_stream(str(e))
            return

        buf = b""
        tc_map = {}
        content_parts = []
        resp_id = ""
        resp_created = int(time.time())
        has_tool_calls = False

        try:
            for raw_chunk in first_resp.iter_content(chunk_size=4096):
                if not raw_chunk:
                    continue
                buf += raw_chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:") or "[DONE]" in line:
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                        if not resp_id:
                            resp_id = data.get("id", "")
                        if data.get("created"):
                            resp_created = data["created"]
                        choice0 = data.get("choices", [{}])[0]
                        delta = choice0.get("delta", {})

                        # Collect tool_calls (do not forward)
                        for tc in delta.get("tool_calls", []):
                            if not has_tool_calls:
                                # First tool_calls detected: immediately send finish_reason:stop,
                                # Make Chatbox think first reply ended normally, no more waiting or timeout retry.
                                # If no text content before, add a space placeholder,
                                # Avoid Chatbox receiving empty message.
                                if not content_parts:
                                    yield _make_sse_chunk(
                                        content=" ", resp_id=resp_id,
                                        created=resp_created, model_id=model_id
                                    )
                                yield _make_sse_chunk(
                                    finish_reason="stop", resp_id=resp_id,
                                    created=resp_created, model_id=model_id
                                )
                                has_tool_calls = True
                            idx = tc.get("index", 0)
                            if idx not in tc_map:
                                tc_map[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                }
                            tc_map[idx]["function"]["arguments"] += \
                                tc.get("function", {}).get("arguments", "")
                            if tc.get("id"):
                                tc_map[idx]["id"] = tc["id"]
                            if tc.get("function", {}).get("name"):
                                tc_map[idx]["function"]["name"] = tc["function"]["name"]

                        # Only forward plain text content
                        if delta.get("content") and not has_tool_calls:
                            content_parts.append(delta["content"])
                            yield (line + "\n\n").encode("utf-8")

                    except Exception:
                        pass
        except Exception as e:
            yield _make_sse_chunk(content=f"[stream interrupted: {str(e)}]", finish_reason="stop", model_id=model_id)
            yield b"data: [DONE]\n\n"
            return
        finally:
            first_resp.close()

        content = "".join(content_parts)
        tool_calls = [tc_map[i] for i in sorted(tc_map)] if tc_map else None

        if not tool_calls:
            log.info("[stream] No tool calls, done")
            # Session managed by Chatbox (stateless server)
            yield _make_sse_chunk(finish_reason="stop", resp_id=resp_id, created=resp_created, model_id=model_id)
            yield b"data: [DONE]\n\n"
            return

        # ---- Multi-round tool calling loop ----
        # Each round: execute tools → send tool name hint → request next round → forward text in real-time
        # Loop until AI stops calling tools.
        MAX_TOOL_ROUNDS = 20  # Prevent infinite loop
        # Force interrupt tool calling when approaching Chatbox total timeout, let AI generate summary directly
        # Chatbox default total timeout ~60-90s; leave 15s margin for final summary generation
        BUDGET_SECONDS = 50
        tool_round = 0
        budget_exceeded = False  # Flag whether interrupted by time budget

        while tool_calls and tool_round < MAX_TOOL_ROUNDS:
            elapsed = time.time() - request_start_time
            # If remaining time insufficient for another tool round + summary, force interrupt
            if elapsed > BUDGET_SECONDS:
                log.warning(f"[BUDGET] Elapsed {elapsed:.1f}s exceeds budget {BUDGET_SECONDS}s, forcing summary")
                # Notify user that tool calling was interrupted by time budget
                budget_resp_id = f"chatcmpl-budget-{int(time.time())}"
                budget_created = int(time.time())
                yield _make_sse_chunk(
                    content="\n[Time budget reached, generating summary based on collected results...]\n\n",
                    resp_id=budget_resp_id, created=budget_created,
                    role="assistant", model_id=model_id
                )
                yield _make_sse_chunk(finish_reason="stop", resp_id=budget_resp_id, created=budget_created, model_id=model_id)
                yield b"data: [DONE]\n\n"

                # Append pending tool_calls as interrupted marker (not actually executed)
                # Then guide AI with a system message to generate summary immediately
                # Note: ensure messages history is valid (assistant tool_calls must pair with tool result)
                # So add an empty tool result for each unexecuted tool_call
                placeholder_results = [
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", "unknown"),
                        "name": tc.get("function", {}).get("name", ""),
                        "content": "[skipped: time budget exceeded]",
                    }
                    for tc in tool_calls
                ]
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls
                })
                messages.extend(placeholder_results)
                messages.append({
                    "role": "system",
                    "content": "Time budget exhausted. Stop calling tools NOW. Summarize the results gathered so far and reply directly to the user in plain text."
                })
                tool_calls = None
                # Let code outside loop send another request to generate summary
                # Use a flag, handle after breaking loop
                budget_exceeded = True
                break

            tool_round += 1
            log.info(f"[TOOL] Round {tool_round}, {len(tool_calls)} call(s): {[tc.get('function',{}).get('name') for tc in tool_calls]}, elapsed={elapsed:.1f}s")

            # Append this round assistant message (with tool_calls) to messages
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls
            })

            # Push tool calling hint to Chatbox (separate message bubble, list all tool names in call order)
            tool_names = [tc.get('function', {}).get('name', 'unknown') for tc in tool_calls]
            # Merge consecutive same names: cmd, cmd, cmd, list -> cmd ×3, list; each tool on separate line
            display_parts = []
            i = 0
            while i < len(tool_names):
                name = tool_names[i]
                count = 1
                while i + count < len(tool_names) and tool_names[i + count] == name:
                    count += 1
                display_parts.append(f"  - {name}" + (f" ×{count}" if count > 1 else ""))
                i += count
            tool_display = "\n".join(display_parts)

            tool_resp_id = f"chatcmpl-tool-{tool_round}-{int(time.time())}"
            tool_created = int(time.time())
            yield _make_sse_chunk(
                content=f"\n\n**Running tools through server:**\n{tool_display}\n\n",
                resp_id=tool_resp_id, created=tool_created,
                role="assistant", model_id=model_id
            )

            # Execute all tools
            tool_results = execute_all_tool_calls(tool_calls)
            messages.extend(tool_results)
            log.info(f"[TOOL] Execution done, result lengths: {[len(r['content']) for r in tool_results]}")

            # End tool execution hint message
            yield _make_sse_chunk(finish_reason="stop", resp_id=tool_resp_id, created=tool_created, model_id=model_id)
            yield b"data: [DONE]\n\n"

            # Send next round request (with tool definitions, AI may continue calling tools)
            log.info(f"[stream] Sending round {tool_round + 1} request, messages={len(messages)}")
            try:
                next_resp = call_llm_stream(messages, tools=tools_schema, model_id=model_id)
            except RuntimeError as e:
                yield from make_error_stream(str(e))
                return

            # Collect next round text and tool_calls; forward text to Chatbox in real-time
            next_content_parts = []
            next_tc_map = {}
            next_has_tool_calls = False
            next_resp_id = ""
            next_created = int(time.time())
            next_buf = b""

            try:
                for chunk in next_resp.iter_content(chunk_size=4096):
                    if not chunk:
                        continue
                    next_buf += chunk
                    while b"\n" in next_buf:
                        line_bytes, next_buf = next_buf.split(b"\n", 1)
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:") or "[DONE]" in line:
                            continue
                        try:
                            d = json.loads(line[5:].strip())
                            if not next_resp_id:
                                next_resp_id = d.get("id", "") or f"chatcmpl-r{tool_round}-{int(time.time())}"
                            if d.get("created"):
                                next_created = d["created"]
                            choice0 = d.get("choices", [{}])[0]
                            delta = choice0.get("delta", {})

                            # Collect tool_calls (do not forward; end current text message on first tool_call)
                            for tc in delta.get("tool_calls", []):
                                if not next_has_tool_calls:
                                    # If text already streamed, end this message first
                                    if next_content_parts:
                                        yield _make_sse_chunk(
                                            finish_reason="stop",
                                            resp_id=next_resp_id,
                                            created=next_created,
                                            model_id=model_id
                                        )
                                        yield b"data: [DONE]\n\n"
                                    next_has_tool_calls = True
                                idx = tc.get("index", 0)
                                if idx not in next_tc_map:
                                    next_tc_map[idx] = {
                                        "id": tc.get("id", ""),
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""}
                                    }
                                next_tc_map[idx]["function"]["arguments"] += \
                                    tc.get("function", {}).get("arguments", "")
                                if tc.get("id"):
                                    next_tc_map[idx]["id"] = tc["id"]
                                if tc.get("function", {}).get("name"):
                                    next_tc_map[idx]["function"]["name"] = tc["function"]["name"]

                            # Text content: forward in real-time (only when tool_calls not yet triggered)
                            if delta.get("content") and not next_has_tool_calls:
                                next_content_parts.append(delta["content"])
                                yield _make_sse_chunk(
                                    content=delta["content"],
                                    resp_id=next_resp_id,
                                    created=next_created,
                                    role="assistant" if len(next_content_parts) == 1 else None,
                                    model_id=model_id
                                )
                        except Exception:
                            pass
            except Exception as e:
                yield _make_sse_chunk(content=f"[stream interrupted: {str(e)}]", finish_reason="stop", model_id=model_id)
                yield b"data: [DONE]\n\n"
                return
            finally:
                next_resp.close()

            content = "".join(next_content_parts)
            tool_calls = [next_tc_map[i] for i in sorted(next_tc_map)] if next_tc_map else None
            resp_id = next_resp_id or resp_id
            resp_created = next_created

        # Loop ended
        if tool_round >= MAX_TOOL_ROUNDS and tool_calls:
            log.warning(f"[TOOL] Reached max tool rounds ({MAX_TOOL_ROUNDS}), stopping")
            yield _make_sse_chunk(content="\n\n[Max tool rounds reached, stopping execution]",
                                  resp_id=resp_id, created=resp_created,
                                  model_id=model_id)

        # Time budget exhausted: send final request without tools to generate summary
        if budget_exceeded:
            log.info("[BUDGET] Sending summary-only request (no tools)")
            summary_resp_id = f"chatcmpl-summary-{int(time.time())}"
            summary_created = int(time.time())
            try:
                summary_resp = call_llm_stream(messages, tools=None, model_id=model_id)
            except RuntimeError as e:
                yield _make_sse_chunk(content=f"\n[summary failed: {str(e)}]",
                                      resp_id=summary_resp_id, created=summary_created,
                                      role="assistant", model_id=model_id)
                summary_resp = None

            if summary_resp is not None:
                summary_collected = []
                summary_buf = b""
                first_text_seen = False
                try:
                    for chunk in summary_resp.iter_content(chunk_size=4096):
                        if not chunk:
                            continue
                        summary_buf += chunk
                        while b"\n" in summary_buf:
                            line_bytes, summary_buf = summary_buf.split(b"\n", 1)
                            line = line_bytes.decode("utf-8", errors="replace").strip()
                            if not line.startswith("data:") or "[DONE]" in line:
                                continue
                            try:
                                d = json.loads(line[5:].strip())
                                delta = d.get("choices", [{}])[0].get("delta", {})
                                text = delta.get("content")
                                if text:
                                    summary_collected.append(text)
                                    yield _make_sse_chunk(
                                        content=text,
                                        resp_id=summary_resp_id, created=summary_created,
                                        role="assistant" if not first_text_seen else None,
                                        model_id=model_id
                                    )
                                    first_text_seen = True
                            except Exception:
                                pass
                except Exception as e:
                    yield _make_sse_chunk(content=f"\n[summary stream interrupted: {str(e)}]",
                                          resp_id=summary_resp_id, created=summary_created,
                                          model_id=model_id)
                finally:
                    summary_resp.close()
                resp_id = summary_resp_id
                resp_created = summary_created

        # Session managed by Chatbox (stateless server)
        yield _make_sse_chunk(finish_reason="stop", resp_id=resp_id, created=resp_created, model_id=model_id)
        yield b"data: [DONE]\n\n"

    return Response(_generate(), content_type='text/event-stream; charset=utf-8')


@app.route('/v1/models', methods=['GET'])
def list_models():
    """OpenAI-compatible model list endpoint, returns all models from models_config.json."""
    models = []
    for provider in MODELS_CONFIG.get("providers", {}).values():
        for model in provider.get("models", []):
            models.append({
                "id": model["id"],
                "object": "model",
                "created": 1700000000,
                "owned_by": provider.get("name", "termux-agent")
            })
    # If config is empty, return at least the default model
    if not models:
        models.append({
            "id": get_default_model_id(),
            "object": "model",
            "created": 1700000000,
            "owned_by": "termux-agent"
        })
    return jsonify({"object": "list", "data": models})


if __name__ == '__main__':
    # ==== Startup configuration prompt ====

    import select
    import sys

    def _mask_key(key: str) -> str:
        """Show first 6 and last 6 chars of an API key, mask the middle."""
        if not key:
            return "(not set)"
        if len(key) <= 12:
            return key
        return key[:6] + "..." + key[-6:]

    def _normalize_url(raw: str) -> str:
        """
        Auto-correct common API Base URL mistakes:
          api.openai.com          -> https://api.openai.com
          http://api.openai.com   -> kept as-is (user chose http)
          https://api.openai.com/ -> https://api.openai.com  (strip trailing slash)
          https://api.openai.com/v1 -> https://api.openai.com  (strip /v1 suffix)
        """
        url = raw.strip()
        if not url:
            return url
        # Add https:// if no scheme present
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        # Strip trailing slash
        url = url.rstrip("/")
        # Strip common path suffixes users accidentally include
        for suffix in ("/v1", "/v1/chat", "/v1/chat/completions"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    def _fetch_models(api_base: str, api_key: str) -> list:
        """
        Call GET /v1/models on the provider and return a list of model dicts.
        Each dict has at least {"id": str, "name": str, "supports_tools": True}.
        Returns an empty list on failure.
        """
        url = f"{api_base.rstrip('/')}/v1/models"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("data", []):
                mid = m.get("id", "").strip()  # Defensively strip leading/trailing spaces from upstream response
                if mid:
                    models.append({
                        "id": mid,
                        "name": mid,
                        "supports_tools": True,
                        "max_tokens": 8192
                    })
            return models
        except Exception as e:
            print(f"  [warn] Failed to fetch models from {url}: {e}")
            return []

    def _print_providers(cfg: dict):
        """Pretty-print all providers with their fetched model lists."""
        providers = cfg.get("providers", {})
        default_model = cfg.get("default_model", "")
        if not providers:
            print("  (no providers configured)")
            return
        for pid, p in providers.items():
            print(f"\n  Provider : {p.get('name', pid)}  [{pid}]")
            print(f"  URL      : {p.get('api_base', '')}")
            print(f"  API Key  : {_mask_key(p.get('api_key', ''))}")
            models = p.get("models", [])
            if models:
                print(f"  Models   :", end="")
                for i, m in enumerate(models):
                    mark = " <- default" if m["id"] == default_model else ""
                    prefix = "           " if i > 0 else " "
                    print(f"{prefix}{m['id']}{mark}")
            else:
                print("  Models   : (none)")

    def _save_config(cfg: dict):
        """Write cfg back to models_config.json."""
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _refresh_all_models(cfg: dict) -> dict:
        """
        For every provider in cfg, call /v1/models and replace the models list.
        Also update default_model to the first model of default_provider if the
        current default_model is no longer in the fetched list.
        """
        for pid, p in cfg.get("providers", {}).items():
            print(f"  Fetching models from {p.get('name', pid)} ...", end=" ", flush=True)
            fetched = _fetch_models(p.get("api_base", ""), p.get("api_key", ""))
            if fetched:
                p["models"] = fetched
                print(f"{len(fetched)} model(s) found")
            else:
                print("failed, keeping existing list")

        # Fix default_model if it no longer exists
        all_ids = [
            m["id"]
            for p in cfg.get("providers", {}).values()
            for m in p.get("models", [])
        ]
        if cfg.get("default_model") not in all_ids and all_ids:
            cfg["default_model"] = all_ids[0]
            default_pid = cfg.get("default_provider", "")
            # Try to pick from default_provider first
            dp_ids = [
                m["id"]
                for m in cfg.get("providers", {}).get(default_pid, {}).get("models", [])
            ]
            if dp_ids:
                cfg["default_model"] = dp_ids[0]
            print(f"  Default model updated to: {cfg['default_model']}")
        return cfg

    def _add_provider_interactive(cfg: dict) -> dict:
        """
        Interactively add a new provider. Used when no valid provider exists.
        Loops until the user provides a non-empty URL and API key.
        """
        print()
        print("=" * 55)
        print("  First-time setup: models_config.json not found")
        print("  Let's configure your first API provider")
        print("=" * 55)
        while True:
            pid   = input("  Provider ID   (short tag, e.g. openai / anthropic / deepseek): ").strip() or "default"
            name  = input(f"  Display name  (e.g. OpenAI / Anthropic / DeepSeek) [{pid}]: ").strip() or pid
            raw_url = input("  API Base URL  (e.g. https://api.openai.com): ").strip()
            url = _normalize_url(raw_url)
            if url != raw_url and raw_url:
                print(f"  [auto-corrected] -> {url}")
            key   = input("  API Key: ").strip()
            if not url or not key:
                print("  URL and API Key are required. Try again.\n")
                continue
            cfg["providers"][pid] = {
                "name": name,
                "api_base": url,
                "api_key": key,
                "models": []
            }
            cfg["default_provider"] = pid
            cfg["default_model"] = ""
            _save_config(cfg)
            print(f"  ✓ Provider '{name}' saved to models_config.json")
            break
        return cfg

    def _startup_prompt():
        """
        Show config info, fetch live model lists, and ask the user to confirm
        before starting the server. Auto-continues after 10 seconds of no input.
        If no provider is configured or all keys are empty, forces the user to
        add one before proceeding.
        Returns the final cfg (possibly modified by the user).
        """
        cfg = _load_models_config()

        # Force setup if there are no providers or every provider has an empty key
        providers = cfg.get("providers", {})
        has_valid = any(p.get("api_key", "").strip() for p in providers.values())
        if not providers or not has_valid:
            cfg = _add_provider_interactive(cfg)

        while True:
            # Fetch live model lists for all providers
            print()
            print("=" * 55)
            print("  Fetching model lists from API...")
            print("=" * 55)
            cfg = _refresh_all_models(cfg)
            _save_config(cfg)

            # If fetch returned nothing for every provider, warn but don't block
            all_models = [
                m for p in cfg.get("providers", {}).values()
                for m in p.get("models", [])
            ]
            if not all_models:
                print("  [warn] No models fetched. Check your URL and API key.")

            print()
            print("=" * 55)
            print("  AIPhoneTools — Startup Config")
            print("=" * 55)
            _print_providers(cfg)
            print()

            print("  Continue? [Y/n]  (auto-yes in 10s)", end=" ", flush=True)

            answered = False
            user_input = ""
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 10)
                if ready:
                    user_input = sys.stdin.readline().strip().lower()
                    answered = True
            except Exception:
                try:
                    user_input = input().strip().lower()
                    answered = True
                except Exception:
                    pass

            if not answered or user_input in ("", "y", "yes"):
                print("  -> Starting server")
                break

            # User wants to edit a provider
            print()
            providers = cfg.get("providers", {})
            pids = list(providers.keys())

            if not pids:
                cfg = _add_provider_interactive(cfg)
                continue
            elif len(pids) == 1:
                pid = pids[0]
            else:
                print("  Select provider to edit:")
                for i, pid_ in enumerate(pids, 1):
                    print(f"    {i}. {providers[pid_].get('name', pid_)}  [{pid_}]")
                while True:
                    sel = input("  Enter number: ").strip()
                    if sel.isdigit() and 1 <= int(sel) <= len(pids):
                        pid = pids[int(sel) - 1]
                        break
                    print("  Invalid input, try again")

            p = providers[pid]
            print(f"\n  Editing: {p.get('name', pid)}")
            print("  (press Enter to keep current value)")

            new_url = input(f"  New URL [{p.get('api_base', '')}]: ").strip()
            new_key = input(f"  New API Key [{_mask_key(p.get('api_key', ''))}]: ").strip()

            if new_url:
                normalized = _normalize_url(new_url)
                if normalized != new_url:
                    print(f"  [auto-corrected] -> {normalized}")
                p["api_base"] = normalized
            if new_key:
                p["api_key"] = new_key

            _save_config(cfg)
            print("  Saved to models_config.json")
            # Loop back to top: re-fetch models and re-display

        return cfg

    # Run startup prompt and update global MODELS_CONFIG
    MODELS_CONFIG = _startup_prompt()

    # Start Flask server
    app.run(host='0.0.0.0', port=5846, debug=False, threaded=True)

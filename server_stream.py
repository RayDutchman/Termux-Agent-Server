import os
import glob
import json
import time
import shutil
import logging
import threading
import subprocess
import requests
from collections import Counter
from flask import Flask, request, jsonify, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ==== 1. 基础配置 ====

DOWNLOAD_DIR = os.path.expanduser("~")

# 工具输出最大字符数，超出截断
TOOL_OUTPUT_MAX_CHARS = 8000
# session 历史最多保留的轮数（一轮 = 一条 user + 一条 assistant）
SESSION_MAX_TURNS = 20

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
    """Load models_config.json, fall back to hardcoded defaults on failure."""
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


# 启动时加载一次；update_models.py 修改配置后需重启服务
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
    model_id = model_id.strip()  # 防御性去除首尾空格
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


# ==== 2. Session 历史管理 ====
# 用 conversation_id（Chatbox 每个会话唯一）作为 key
# 存储完整的 messages 列表，包含 tool_calls 和 tool results
_sessions: dict = {}
_sessions_lock = threading.Lock()


def _get_session(conv_id: str) -> list:
    with _sessions_lock:
        history = list(_sessions.get(conv_id, []))

    # Clean up orphaned tool_calls at the end of session history.
    # If the last message is an assistant message with tool_calls but no following
    # tool result, the previous execution was interrupted (Chatbox interception or
    # network error). Trim the dangling tail to prevent infinite retry loops.
    cleaned = list(history)
    while cleaned:
        last = cleaned[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            log.warning(f"[SESSION] Orphaned tool_calls detected, cleaning conv_id={conv_id!r}")
            cleaned.pop()
        elif last.get("role") == "tool":
            # tool result without a following assistant reply — also trim
            cleaned.pop()
        else:
            break
    if len(cleaned) != len(history):
        log.warning(f"[SESSION] Removed {len(history) - len(cleaned)} orphaned message(s)")
        with _sessions_lock:
            _sessions[conv_id] = cleaned
    return cleaned


def _save_session(conv_id: str, messages: list):
    """
    Save history. Trim oldest turns when SESSION_MAX_TURNS is exceeded.
    A "turn" starts at a user message; trimming preserves complete turns
    so we never split an assistant/tool_calls/tool_result group.
    """
    with _sessions_lock:
        history = [m for m in messages if m.get("role") != "system"]
        before_trim = len(history)

        # Find the indices of all user messages — these are turn boundaries
        user_indices = [i for i, m in enumerate(history) if m.get("role") == "user"]
        if len(user_indices) > SESSION_MAX_TURNS:
            # Keep only the last SESSION_MAX_TURNS turns (start at the Nth user msg from end)
            cut_at = user_indices[-SESSION_MAX_TURNS]
            history = history[cut_at:]

        _sessions[conv_id] = history
        log.info(f"[SESSION] Saved conv_id={conv_id!r}, messages: {before_trim} -> {len(history)}")


def _clear_session(conv_id: str):
    with _sessions_lock:
        _sessions.pop(conv_id, None)



# ==== 3. 工具函数 ====

def read_phone_file(filename):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        size = os.path.getsize(path)
        if size > 500 * 1024:
            return f"错误: 文件 {filename} 过大 ({size // 1024}KB)，请指定更小的文件"
        # 优先 UTF-8，失败后降级 latin-1（保证不抛异常）
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        return f"错误: 无法以任何已知编码读取文件 {filename}"
    except FileNotFoundError:
        return f"错误: 文件不存在: {filename}"
    except Exception as e:
        return f"错误: 无法读取文件 {filename}。原因: {str(e)}"


def write_phone_file(filename, content):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功: 已保存至 {path}"
    except Exception as e:
        return f"错误: 无法写入文件。原因: {str(e)}"


def execute_local_command(command=None, **kwargs):
    if command is None:
        command = kwargs.get("cmd") or kwargs.get("shell_command") or kwargs.get("shell") or ""
    if not command:
        return "错误: 未提供命令"
    try:
        result = subprocess.run(
            command, shell=True, text=True, capture_output=True, timeout=30
        )
        output = f"【退出码】: {result.returncode}\n【标准输出】:\n{result.stdout}\n【标准错误】:\n{result.stderr}"
        if len(output) > TOOL_OUTPUT_MAX_CHARS:
            output = output[:TOOL_OUTPUT_MAX_CHARS] + f"\n...[输出过长，已截断至 {TOOL_OUTPUT_MAX_CHARS} 字符]"
        return output
    except subprocess.TimeoutExpired:
        return "错误: 命令执行超时（30秒）"
    except Exception as e:
        return f"错误: 命令执行失败。原因: {str(e)}"


def list_phone_dir(sub_dir=""):
    target_dir = os.path.join(DOWNLOAD_DIR, sub_dir) if sub_dir else DOWNLOAD_DIR
    try:
        if not os.path.exists(target_dir):
            return f"错误: 目录不存在: {target_dir}"
        items = os.listdir(target_dir)
        return json.dumps(
            {"directory": target_dir, "contents": items, "count": len(items)},
            ensure_ascii=False, indent=2
        )
    except Exception as e:
        return f"错误: 无法列出目录。原因: {str(e)}"


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
        return f"错误: 搜索失败。原因: {str(e)}"


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
            status["battery"] = "Termux-API 未安装或无法获取电量信息"

        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: 获取系统状态失败。原因: {str(e)}"


tools_map = {
    "read_phone_file": read_phone_file,
    "write_phone_file": write_phone_file,
    "execute_local_command": execute_local_command,
    "list_phone_dir": list_phone_dir,
    "search_phone_files": search_phone_files,
    "get_phone_system_status": get_phone_system_status,
}

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "read_phone_file",
            "description": "读取手机 Termux home 目录下的文本、CSV 或代码文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名，例如 data.csv 或子目录/文件名"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_phone_file",
            "description": "在手机 Termux home 目录写入或创建文件，支持子目录（自动创建）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "保存的文件名，例如 summary.txt 或 notes/todo.txt"},
                    "content": {"type": "string", "description": "写入文件的具体内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_local_command",
            "description": "在手机 Termux 环境内执行 shell 命令，如运行脚本、pip 安装、查看目录等",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令，例如 ls ~、python script.py"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_phone_dir",
            "description": "列出 Termux home 目录或其子目录的文件和文件夹",
            "parameters": {
                "type": "object",
                "properties": {
                    "sub_dir": {
                        "type": "string",
                        "description": "子目录名，例如 'projects'。留空则列出 home 根目录"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_phone_files",
            "description": "在 Termux home 目录中按通配符搜索文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_pattern": {
                        "type": "string",
                        "description": "搜索模式，例如 '*.py'、'*.pdf'、'*invoice*'"
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
            "description": "获取手机当前存储空间、内存使用情况和电量状态",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]


# ==== 4. LLM 请求 ====

def make_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def safe_get_choice(data):
    """安全获取 choices[0].message，返回 dict 或空 dict。"""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return {}
    return choices[0].get("message") or {}


def call_llm_sync(messages, tools=None, model_id: str = None):
    """
    非流式请求，返回解析后的 dict。
    model_id 为空时使用默认模型。
    """
    model_id = model_id or get_default_model_id()
    provider, model = get_provider_for_model(model_id)

    api_url = f"{provider['api_base']}/v1/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": False}
    # 只有模型声明支持工具时才带 tools 字段
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
    流式请求，返回 requests.Response 对象（未读取 body）。
    model_id 为空时使用默认模型。
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


# ==== 5. 工具执行 ====

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

        # 空参数直接用 {}，不走 JSON 解析（避免无意义的 WARNING）
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


# ==== 6. SSE 工具函数 ====

def _make_sse_chunk(content=None, finish_reason=None, resp_id="", role=None, created=None, model_id=None):
    """
    构造一个符合 OpenAI 规范的 SSE data chunk 字节串。
    规范要求必填字段：id, object, created, model, choices
    choices[] 必填：index, delta, finish_reason（可为 null）
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


# ==== 7. 路由 ====

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    chatbox_data = request.json or {}
    want_stream = chatbox_data.get("stream", False)

    # 提取 model_id，未指定时使用默认模型
    model_id = (chatbox_data.get("model") or get_default_model_id()).strip()

    # 增强 conv_id 提取逻辑（尝试多种字段名）
    conv_id = (
        chatbox_data.get("conversation_id") or 
        chatbox_data.get("id") or 
        chatbox_data.get("conversationId") or
        chatbox_data.get("session_id") or
        ""
    )

    # 提取 Chatbox 发来的 system 消息（如果有）
    chatbox_system_msgs = [m for m in chatbox_data.get("messages", []) if m.get("role") == "system"]
    incoming = [m for m in chatbox_data.get("messages", []) if m.get("role") != "system"]
    latest_user_msg = next((m for m in reversed(incoming) if m["role"] == "user"), None)
    
    # 如果 conv_id 还是空，用 user 消息的 hash 作为 fallback
    if not conv_id and incoming:
        import hashlib
        first_user = next((m for m in incoming if m.get("role") == "user"), None)
        if first_user:
            conv_id = "fallback-" + hashlib.md5(
                str(first_user.get("content", ""))[:100].encode()
            ).hexdigest()[:8]
    
    log.info(f"[REQUEST] conv_id={conv_id!r}, model={model_id!r}, stream={want_stream}, incoming_msgs={len(incoming)}")
    if latest_user_msg:
        user_content = latest_user_msg.get("content", "")[:50]
        log.info(f"[REQUEST] latest_user_msg: {user_content}...")

    # 构建 system prompt：基础说明 + memory.md + Chatbox 的 system 消息
    system_parts = []
    
    # 1. 基础 system prompt
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
    
    # 2. 自动加载 memory.md（如果存在）
    memory_path = os.path.join(DOWNLOAD_DIR, "memory.md")
    if os.path.exists(memory_path):
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory_content = f.read(2000)  # 限制 2000 字符
            if memory_content.strip():
                system_parts.append(f"\n\n--- Long-term Memory (from ~/memory.md) ---\n{memory_content}")
                log.info(f"[MEMORY] Loaded {len(memory_content)} chars from memory.md")
        except Exception as e:
            log.warning(f"[MEMORY] Failed to load memory.md: {e}")
    
    # 3. 合并 Chatbox 发来的 system 消息（如果有）
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

    server_history = _get_session(conv_id) if conv_id else []

    # Check recent history to avoid appending duplicate user messages
    # (e.g. when the user resends the same message multiple times)
    if latest_user_msg:
        latest_content = latest_user_msg.get("content", "")
        recent = server_history[-6:] if len(server_history) >= 6 else server_history
        already_in_history = any(
            m.get("role") == "user" and m.get("content", "") == latest_content
            for m in recent
        )
        if not already_in_history:
            server_history.append(latest_user_msg)
            log.info("[SESSION] Appended new user message")
        else:
            log.info("[SESSION] Duplicate user message detected, skipping append")

    log.info(f"[SESSION] history_length={len(server_history)}")

    messages = [system_prompt] + server_history

    # ---- 非流式模式：多轮工具调用循环 ----
    if not want_stream:
        MAX_TOOL_ROUNDS = 10
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

        if conv_id:
            _save_session(conv_id, messages + [safe_get_choice(current_data)])
        return Response(
            json.dumps(current_data, ensure_ascii=False),
            content_type='application/json; charset=utf-8'
        )

    # ---- 流式模式：边收边发，立刻响应 Chatbox ----
    def _generate():
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

                        # 收集 tool_calls（不转发）
                        for tc in delta.get("tool_calls", []):
                            if not has_tool_calls:
                                # 第一次检测到 tool_calls：立刻补发 finish_reason:stop，
                                # 让 Chatbox 认为第一段回复已正常结束，不再等待也不超时重试。
                                # 如果前面没有任何文字内容，补一个空格占位，
                                # 避免 Chatbox 收到空消息。
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

                        # 只转发纯文本 content
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
            if conv_id:
                _save_session(conv_id, messages + [{"role": "assistant", "content": content}])
            yield _make_sse_chunk(finish_reason="stop", resp_id=resp_id, created=resp_created, model_id=model_id)
            yield b"data: [DONE]\n\n"
            return

        # ---- 多轮工具调用循环 ----
        # 每轮：执行工具 → 发工具名提示 → 请求下一轮 → 实时流式转发文字
        # 直到 AI 不再调工具，循环结束。
        MAX_TOOL_ROUNDS = 10  # 防止死循环
        tool_round = 0
        last_round_text = ""  # 最后一轮收到的纯文本（用于 session 保存）

        while tool_calls and tool_round < MAX_TOOL_ROUNDS:
            tool_round += 1
            log.info(f"[TOOL] Round {tool_round}, {len(tool_calls)} call(s): {[tc.get('function',{}).get('name') for tc in tool_calls]}")

            # 把本轮 assistant 消息（含 tool_calls）和工具结果追加到 messages
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls
            })
            tool_results = execute_all_tool_calls(tool_calls)
            messages.extend(tool_results)
            log.info(f"[TOOL] Execution done, result lengths: {[len(r['content']) for r in tool_results]}")

            # 向 Chatbox 推送工具调用提示（独立消息气泡，显示工具名）
            tool_names = [tc.get('function', {}).get('name', 'unknown') for tc in tool_calls]
            # 同名工具合并显示次数：[read_phone_file ×2, list_phone_dir]
            name_counts = Counter(tool_names)
            display_parts = []
            for name in dict.fromkeys(tool_names):  # 保留顺序去重
                count = name_counts[name]
                display_parts.append(f"{name}" + (f" ×{count}" if count > 1 else ""))
            tool_display = ", ".join(display_parts)

            tool_resp_id = f"chatcmpl-tool-{tool_round}-{int(time.time())}"
            tool_created = int(time.time())
            yield _make_sse_chunk(
                content=f"Running: {tool_display}\n\n",
                resp_id=tool_resp_id, created=tool_created,
                role="assistant", model_id=model_id
            )
            yield _make_sse_chunk(finish_reason="stop", resp_id=tool_resp_id, created=tool_created, model_id=model_id)
            yield b"data: [DONE]\n\n"

            # 发起下一轮请求（带工具定义，AI 可能继续调工具）
            log.info(f"[stream] Sending round {tool_round + 1} request, messages={len(messages)}")
            try:
                next_resp = call_llm_stream(messages, tools=tools_schema, model_id=model_id)
            except RuntimeError as e:
                yield from make_error_stream(str(e))
                return

            # 收集下一轮的文字和 tool_calls；文字实时转发给 Chatbox
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

                            # 收集 tool_calls（不转发；遇到第一个 tool_call 就结束当前文字消息）
                            for tc in delta.get("tool_calls", []):
                                if not next_has_tool_calls:
                                    # 如果已经流式发出过文字，先结束这条消息
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

                            # 文字内容：实时转发（仅在尚未触发 tool_calls 时）
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
            last_round_text = content

        # 循环结束
        if tool_round >= MAX_TOOL_ROUNDS and tool_calls:
            log.warning(f"[TOOL] Reached max tool rounds ({MAX_TOOL_ROUNDS}), stopping")
            yield _make_sse_chunk(content="\n\n[工具调用轮次已达上限，停止执行]",
                                  resp_id=resp_id, created=resp_created,
                                  model_id=model_id)

        if conv_id:
            _save_session(conv_id, messages + [{"role": "assistant", "content": last_round_text}])

        yield _make_sse_chunk(finish_reason="stop", resp_id=resp_id, created=resp_created, model_id=model_id)
        yield b"data: [DONE]\n\n"

    return Response(_generate(), content_type='text/event-stream; charset=utf-8')


@app.route('/v1/models', methods=['GET'])
def list_models():
    """OpenAI 兼容的模型列表端点，返回 models_config.json 中所有模型。"""
    models = []
    for provider in MODELS_CONFIG.get("providers", {}).values():
        for model in provider.get("models", []):
            models.append({
                "id": model["id"],
                "object": "model",
                "created": 1700000000,
                "owned_by": provider.get("name", "termux-agent")
            })
    # 如果配置为空，至少返回默认模型
    if not models:
        models.append({
            "id": get_default_model_id(),
            "object": "model",
            "created": 1700000000,
            "owned_by": "termux-agent"
        })
    return jsonify({"object": "list", "data": models})


@app.route('/v1/sessions', methods=['DELETE'])
def clear_sessions():
    """Clear all session history (debug use)."""
    with _sessions_lock:
        _sessions.clear()
    return jsonify({"status": "ok", "message": "All sessions cleared"})


@app.route('/v1/sessions/<conv_id>', methods=['DELETE'])
def clear_session(conv_id):
    """Clear a specific session."""
    _clear_session(conv_id)
    return jsonify({"status": "ok", "message": f"Session {conv_id} cleared"})


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
                mid = m.get("id", "").strip()  # 防御性去除上游返回的首尾空格
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
        print("  No valid provider found. Please configure one now.")
        print("  -----------------------------------------------")
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
            print(f"  Provider '{name}' saved.")
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
            print("  Termux Agent Server — Startup Config")
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

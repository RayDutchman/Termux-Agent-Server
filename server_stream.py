import os
import glob
import json
import time
import shutil
import logging
import threading
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

# ==== 1. 基础配置 ====

DOWNLOAD_DIR = os.path.expanduser("~")

# 工具输出最大字符数，超出截断
TOOL_OUTPUT_MAX_CHARS = 8000
# session 历史最多保留的轮数（一轮 = 一条 user + 一条 assistant）
SESSION_MAX_TURNS = 20

# ==== 1b. 多模型配置加载 ====
# 优先读取 models_config.json，不存在时回退到硬编码默认值

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_config.json")

# 硬编码兜底配置（models_config.json 不存在时使用）
_FALLBACK_CONFIG = {
    "providers": {
        "lmuai": {
            "name": "LMU AI",
            "api_base": "https://api.lmuai.com",
            "api_key": "",
            "models": [
                {
                    "id": "claude-sonnet-4-6",
                    "name": "Claude Sonnet 4.6",
                    "supports_tools": True,
                    "max_tokens": 8192
                }
            ]
        }
    },
    "default_provider": "lmuai",
    "default_model": "claude-sonnet-4-6"
}


def _load_models_config() -> dict:
    """加载 models_config.json，失败时返回兜底配置。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        log.info(f"[CONFIG] 已加载 models_config.json，providers={list(cfg.get('providers', {}).keys())}")
        return cfg
    except FileNotFoundError:
        log.warning(f"[CONFIG] models_config.json 不存在，使用兜底配置")
        return _FALLBACK_CONFIG
    except Exception as e:
        log.error(f"[CONFIG] 加载 models_config.json 失败: {e}，使用兜底配置")
        return _FALLBACK_CONFIG


# 启动时加载一次；update_models.py 修改配置后需重启服务
MODELS_CONFIG: dict = _load_models_config()


def get_provider_for_model(model_id: str):
    """
    根据 model_id 查找对应的 provider 配置和 model 配置。
    返回 (provider_dict, model_dict)，找不到时返回 (default_provider, default_model)。
    """
    for provider in MODELS_CONFIG.get("providers", {}).values():
        for model in provider.get("models", []):
            if model["id"] == model_id:
                return provider, model

    # 找不到时使用默认 provider + 默认 model
    default_provider_id = MODELS_CONFIG.get("default_provider", "")
    default_model_id = MODELS_CONFIG.get("default_model", "")
    default_provider = MODELS_CONFIG.get("providers", {}).get(default_provider_id, {})
    default_model = next(
        (m for m in default_provider.get("models", []) if m["id"] == default_model_id),
        {"id": default_model_id, "supports_tools": True, "max_tokens": 8192}
    )
    log.warning(f"[CONFIG] 模型 {model_id!r} 未找到，回退到默认: {default_model_id!r}")
    return default_provider, default_model


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

    # 清理末尾孤立的 tool_calls：
    # 如果 session 末尾有 assistant 消息带 tool_calls，但后面没有对应的 tool result，
    # 说明上次工具执行被中断（Chatbox 拦截或网络错误），直接截掉这段残缺历史，
    # 避免 AI 下次看到未完成的 tool_calls 而无限重试。
    cleaned = list(history)
    while cleaned:
        last = cleaned[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            # 末尾是带 tool_calls 的 assistant 消息，属于孤立状态
            log.warning(f"[SESSION] 检测到末尾孤立 tool_calls，清理 conv_id={conv_id!r}")
            cleaned.pop()
        elif last.get("role") == "tool":
            # tool result 后面应该跟着 assistant 回复，如果没有也清理
            cleaned.pop()
        else:
            break
    if len(cleaned) != len(history):
        log.warning(f"[SESSION] 清理了 {len(history) - len(cleaned)} 条孤立消息")
        with _sessions_lock:
            _sessions[conv_id] = cleaned
    return cleaned


def _save_session(conv_id: str, messages: list):
    """保存历史，超出 SESSION_MAX_TURNS 时裁剪最早的轮次（保留 system prompt）。"""
    with _sessions_lock:
        # messages[0] 是 system prompt，从 [1:] 开始算轮次
        history = [m for m in messages if m.get("role") != "system"]
        # 每轮至少包含 user + assistant，粗略按消息数裁剪
        max_msgs = SESSION_MAX_TURNS * 4  # user + assistant(tool_calls) + tool + assistant
        before_trim = len(history)
        if len(history) > max_msgs:
            history = history[-max_msgs:]
        _sessions[conv_id] = history
        
        # === 添加保存日志 ===
        log.info(f"[SESSION] 保存 conv_id={conv_id!r}, 消息数: {before_trim} -> {len(history)}")


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
    log.info(f"[sync] model={model_id!r}, provider={provider.get('name')}, 消息数={len(messages)}, 带工具={tools is not None}")
    # 打印最后 3 条消息摘要
    if len(messages) > 1:
        for i, msg in enumerate(messages[-3:]):
            role = msg.get("role", "")
            content_preview = str(msg.get("content", ""))[:80] if msg.get("content") else ""
            has_tool_calls = "tool_calls" in msg
            log.info(f"  msg[{len(messages)-3+i}]: role={role}, has_tool_calls={has_tool_calls}, content={content_preview}...")
    try:
        resp = requests.post(
            api_url, json=payload,
            headers=make_headers(provider["api_key"]),
            timeout=120
        )
        resp.raise_for_status()
        result = json.loads(resp.content.decode("utf-8"))
        log.info(f"[sync] 完成，耗时={time.time()-t0:.1f}s")
        return result
    except requests.exceptions.Timeout:
        raise RuntimeError("上游 LLM 请求超时")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"上游 LLM 返回错误: {e.response.status_code} {e.response.text[:200]}")
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        raise RuntimeError(f"请求或解析失败: {str(e)}")


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

    log.info(f"[stream] model={model_id!r}, provider={provider.get('name')}, 消息数={len(messages)}")
    try:
        resp = requests.post(
            api_url, json=payload,
            headers=make_headers(provider["api_key"]),
            stream=True, timeout=(30, 300)
        )
        resp.raise_for_status()
        return resp
    except requests.exceptions.Timeout:
        raise RuntimeError("上游 LLM 请求超时")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"上游 LLM 返回错误: {e.response.status_code} {e.response.text[:200]}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求失败: {str(e)}")


# ==== 5. 工具执行 ====

def execute_all_tool_calls(tool_calls):
    """执行所有 tool_calls，返回 tool 消息列表。"""
    results = []
    for tool_call in tool_calls:
        func_info = tool_call.get("function", {})
        func_name = func_info.get("name", "")
        tool_call_id = tool_call.get("id", "unknown")

        # === 添加参数解析日志 ===
        raw_arguments = func_info.get("arguments", "{}")
        log.info(f"[TOOL_EXEC] {func_name}, raw_arguments={raw_arguments[:200]}")
        
        # === 修复上游 API 返回的错误格式：去掉开头的 {} ===
        if raw_arguments.startswith("{}"):
            raw_arguments = raw_arguments[2:]
            log.info(f"[TOOL_EXEC] 检测到并移除开头的 {{}}, 修正后: {raw_arguments[:200]}")
        
        try:
            func_args = json.loads(raw_arguments)
            if not isinstance(func_args, dict):
                func_args = {}
        except json.JSONDecodeError as e:
            log.warning(f"[TOOL_EXEC] JSON 解析失败: {e}")
            func_args = {}
        
        log.info(f"[TOOL_EXEC] {func_name}, parsed_args={func_args}")

        if not func_name:
            result = "错误: 工具名称为空"
        elif func_name in tools_map:
            try:
                result = tools_map[func_name](**func_args)
            except TypeError as e:
                result = f"错误: 工具参数不匹配 ({str(e)})，收到的参数: {func_args}"
            except Exception as e:
                result = f"错误: 工具执行异常: {str(e)}"
        else:
            result = f"错误: 未知工具 {func_name}"

        result_str = str(result)
        if len(result_str) > TOOL_OUTPUT_MAX_CHARS:
            result_str = result_str[:TOOL_OUTPUT_MAX_CHARS] + "\n...[已截断]"

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


def stream_proxy(upstream_resp):
    """
    按 SSE 事件边界透传上游流。
    SSE 协议每个事件以 \\n\\n 结尾，iter_content 按原始字节块传输，
    不会在 JSON 内容中间截断，是最安全的透传方式。
    """
    try:
        for chunk in upstream_resp.iter_content(chunk_size=4096):
            if chunk:
                yield chunk
    except Exception as e:
        yield _make_sse_chunk(content=f"[流式传输中断: {str(e)}]", finish_reason="stop")
        yield b"data: [DONE]\n\n"
    finally:
        upstream_resp.close()


def wrap_as_stream(data):
    """把非流式响应 dict 包装成合规的 SSE chunk 序列，不重复请求。"""
    choices = data.get("choices", [])
    resp_id = data.get("id", "")
    created = data.get("created", int(time.time()))
    content = (choices[0].get("message", {}).get("content") or "") if choices else ""
    finish_reason = (choices[0].get("finish_reason") or "stop") if choices else "stop"

    # 第一个 chunk：role + content
    yield _make_sse_chunk(content=content, resp_id=resp_id, role="assistant", created=created)
    # 最后一个 chunk：finish_reason，delta 为空
    yield _make_sse_chunk(finish_reason=finish_reason, resp_id=resp_id, created=created)
    yield b"data: [DONE]\n\n"


def make_error_stream(message):
    """把错误信息包装成合法的 SSE chunk 序列。"""
    err_id = f"err-{int(time.time())}"
    yield _make_sse_chunk(content=f"[错误] {message}", resp_id=err_id, role="assistant")
    yield _make_sse_chunk(finish_reason="stop", resp_id=err_id)
    yield b"data: [DONE]\n\n"


# ==== 7. 路由 ====

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    chatbox_data = request.json or {}
    want_stream = chatbox_data.get("stream", False)

    # 提取 model_id，未指定时使用默认模型
    model_id = chatbox_data.get("model") or get_default_model_id()

    # 增强 conv_id 提取逻辑（尝试多种字段名）
    conv_id = (
        chatbox_data.get("conversation_id") or 
        chatbox_data.get("id") or 
        chatbox_data.get("conversationId") or
        chatbox_data.get("session_id") or
        ""
    )

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

    system_prompt = {
        "role": "system",
        "content": (
            f"你是一个运行在安卓手机 Termux 里的多功能 AI 助手。"
            f"你的工作目录是 {DOWNLOAD_DIR}。"
            f"需要操作文件或执行命令时，请调用对应工具；"
            f"普通对话直接回复即可，不要无谓地调用工具。"
        )
    }

    server_history = _get_session(conv_id) if conv_id else []

    # 追加最新 user 消息前，检查最近几条历史里是否已经有完全相同的内容，
    # 避免用户重发同一条消息时在 session 里重复堆积。
    if latest_user_msg:
        latest_content = latest_user_msg.get("content", "")
        # 检查最近 6 条消息里有没有相同内容的 user 消息
        recent = server_history[-6:] if len(server_history) >= 6 else server_history
        already_in_history = any(
            m.get("role") == "user" and m.get("content", "") == latest_content
            for m in recent
        )
        if not already_in_history:
            server_history.append(latest_user_msg)
            log.info(f"[SESSION] 追加新 user 消息")
        else:
            log.info(f"[SESSION] user 消息已在近期历史中，跳过追加（防重复）")

    log.info(f"[SESSION] history_length={len(server_history)}")

    messages = [system_prompt] + server_history

    # ---- 非流式模式 ----
    if not want_stream:
        try:
            first_data = call_llm_sync(messages, tools=tools_schema, model_id=model_id)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 502

        choice = safe_get_choice(first_data)

        if choice.get("tool_calls"):
            log.info(f"[TOOL] 检测到 {len(choice['tool_calls'])} 个工具调用:")
            for i, tc in enumerate(choice["tool_calls"]):
                func_name = tc.get("function", {}).get("name", "")
                func_args_raw = tc.get("function", {}).get("arguments", "")
                log.info(f"  [{i+1}] {func_name}")
                log.info(f"      raw_args: {repr(func_args_raw[:150])}")
            
            messages.append(choice)
            tool_results = execute_all_tool_calls(choice["tool_calls"])
            messages.extend(tool_results)
            
            log.info(f"[TOOL] 执行完毕，结果长度: {[len(r['content']) for r in tool_results]}")
            log.info(f"[tool] 发起第二轮请求")
            try:
                second_data = call_llm_sync(messages, model_id=model_id)
                if conv_id:
                    _save_session(conv_id, messages + [safe_get_choice(second_data)])
                return Response(
                    json.dumps(second_data, ensure_ascii=False),
                    content_type='application/json; charset=utf-8'
                )
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 502

        if conv_id:
            _save_session(conv_id, messages + [choice])
        return Response(
            json.dumps(first_data, ensure_ascii=False),
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
                    if not line.startswith("data:"):
                        continue
                    if "[DONE]" in line:
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
            yield _make_sse_chunk(content=f"[流式传输中断: {str(e)}]", finish_reason="stop", model_id=model_id)
            yield b"data: [DONE]\n\n"
            return
        finally:
            first_resp.close()

        content = "".join(content_parts)
        tool_calls = [tc_map[i] for i in sorted(tc_map)] if tc_map else None

        if not tool_calls:
            log.info("[stream] 无工具调用，完成")
            if conv_id:
                _save_session(conv_id, messages + [{"role": "assistant", "content": content}])
            yield _make_sse_chunk(finish_reason="stop", resp_id=resp_id, created=resp_created, model_id=model_id)
            yield b"data: [DONE]\n\n"
            return

        # 有工具调用：执行工具，第二轮结果追加到同一个流
        log.info(f"[TOOL] 检测到 {len(tool_calls)} 个工具调用:")
        for i, tc in enumerate(tool_calls):
            func_name = tc.get("function", {}).get("name", "")
            func_args_raw = tc.get("function", {}).get("arguments", "")
            log.info(f"  [{i+1}] {func_name}")
            log.info(f"      raw_args: {repr(func_args_raw[:150])}")

        # 用新的 resp_id 开启第二段消息，让 Chatbox 把工具执行结果当成独立回复
        tool_resp_id = f"chatcmpl-tool-{int(time.time())}"
        tool_created = int(time.time())
        yield _make_sse_chunk(
            content="⚙️ 正在执行工具...\n\n",
            resp_id=tool_resp_id, created=tool_created,
            role="assistant", model_id=model_id
        )

        assistant_msg = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls
        }
        messages.append(assistant_msg)
        tool_results = execute_all_tool_calls(tool_calls)
        messages.extend(tool_results)
        
        log.info(f"[TOOL] 执行完毕，结果长度: {[len(r['content']) for r in tool_results]}")
        log.info("[stream] 发起第二轮流式请求")

        try:
            second_resp = call_llm_stream(messages, model_id=model_id)
        except RuntimeError as e:
            yield from make_error_stream(str(e))
            return

        collected = []
        buf2 = b""
        try:
            for chunk in second_resp.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                yield chunk
                buf2 += chunk
                while b"\n" in buf2:
                    line_bytes, buf2 = buf2.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:") and "[DONE]" not in line:
                        try:
                            d = json.loads(line[5:].strip())
                            delta = d.get("choices", [{}])[0].get("delta", {})
                            if delta.get("content"):
                                collected.append(delta["content"])
                        except Exception:
                            pass
        except Exception as e:
            yield _make_sse_chunk(content=f"[流式传输中断: {str(e)}]", finish_reason="stop", model_id=model_id)
            yield b"data: [DONE]\n\n"
        finally:
            second_resp.close()
            if conv_id:
                _save_session(conv_id, messages + [{"role": "assistant", "content": "".join(collected)}])

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
    """清空所有 session 历史，调试用。"""
    with _sessions_lock:
        _sessions.clear()
    return jsonify({"status": "ok", "message": "所有 session 已清空"})


@app.route('/v1/sessions/<conv_id>', methods=['DELETE'])
def clear_session(conv_id):
    """清空指定 session 历史。"""
    _clear_session(conv_id)
    return jsonify({"status": "ok", "message": f"session {conv_id} 已清空"})


if __name__ == '__main__':
    # 监听所有网络接口，允许局域网访问
    app.run(host='0.0.0.0', port=5846, debug=False, threaded=True)

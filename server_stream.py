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
API_KEY = "sk-9b7a62c8cee0aa73203584c7f2d39d1e0adb507dd7c90b8f34f9bbf073a966f7"
API_BASE = "https://api.lmuai.com"
API_URL = f"{API_BASE}/v1/chat/completions"
MODEL_NAME = "claude-sonnet-4-6"

DOWNLOAD_DIR = os.path.expanduser("~")

# 工具输出最大字符数，超出截断
TOOL_OUTPUT_MAX_CHARS = 8000
# session 历史最多保留的轮数（一轮 = 一条 user + 一条 assistant）
SESSION_MAX_TURNS = 20


# ==== 2. Session 历史管理 ====
# 用 conversation_id（Chatbox 每个会话唯一）作为 key
# 存储完整的 messages 列表，包含 tool_calls 和 tool results
_sessions: dict = {}
_sessions_lock = threading.Lock()


def _get_session(conv_id: str) -> list:
    with _sessions_lock:
        return list(_sessions.get(conv_id, []))


def _save_session(conv_id: str, messages: list):
    """保存历史，超出 SESSION_MAX_TURNS 时裁剪最早的轮次（保留 system prompt）。"""
    with _sessions_lock:
        # messages[0] 是 system prompt，从 [1:] 开始算轮次
        history = [m for m in messages if m.get("role") != "system"]
        # 每轮至少包含 user + assistant，粗略按消息数裁剪
        max_msgs = SESSION_MAX_TURNS * 4  # user + assistant(tool_calls) + tool + assistant
        if len(history) > max_msgs:
            history = history[-max_msgs:]
        _sessions[conv_id] = history


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

def make_headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def safe_get_choice(data):
    """安全获取 choices[0].message，返回 dict 或空 dict。"""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return {}
    return choices[0].get("message") or {}


def call_llm_sync(messages, tools=None):
    """非流式请求，返回解析后的 dict。"""
    payload = {"model": MODEL_NAME, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    t0 = time.time()
    log.info(f"[sync] 发送请求，消息数={len(messages)}, 带工具={tools is not None}")
    try:
        resp = requests.post(API_URL, json=payload, headers=make_headers(), timeout=120)
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


def call_llm_stream(messages, tools=None):
    """流式请求，返回 requests.Response 对象（未读取 body）。"""
    payload = {"model": MODEL_NAME, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
    log.info(f"[stream] 发送请求，消息数={len(messages)}")
    try:
        resp = requests.post(
            API_URL, json=payload, headers=make_headers(),
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

        try:
            func_args = json.loads(func_info.get("arguments", "{}"))
            if not isinstance(func_args, dict):
                func_args = {}
        except json.JSONDecodeError:
            func_args = {}

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

def _make_sse_chunk(content=None, finish_reason=None, resp_id="", role=None):
    """构造一个标准 SSE data chunk 的字节串。"""
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    chunk = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "model": MODEL_NAME,
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
    """把非流式响应 dict 包装成 SSE，不重复请求。"""
    choices = data.get("choices", [])
    resp_id = data.get("id", "")
    content = (choices[0].get("message", {}).get("content") or "") if choices else ""
    finish_reason = (choices[0].get("finish_reason") or "stop") if choices else "stop"

    yield _make_sse_chunk(content=content, resp_id=resp_id, role="assistant")
    yield _make_sse_chunk(finish_reason=finish_reason, resp_id=resp_id)
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

    # Chatbox 用 conversation_id 标识会话，没有则退化为无记忆模式
    conv_id = chatbox_data.get("conversation_id") or chatbox_data.get("id") or ""

    # 从请求中提取最新一条 user 消息
    incoming = [m for m in chatbox_data.get("messages", []) if m.get("role") != "system"]
    latest_user_msg = next((m for m in reversed(incoming) if m["role"] == "user"), None)

    system_prompt = {
        "role": "system",
        "content": (
            f"你是一个运行在安卓手机 Termux 里的多功能 AI 助手。"
            f"你的工作目录是 {DOWNLOAD_DIR}。"
            f"需要操作文件或执行命令时，请调用对应工具；"
            f"普通对话直接回复即可，不要无谓地调用工具。"
        )
    }

    # 用服务端历史重建完整 messages（包含历史 tool_calls 和 tool results）
    server_history = _get_session(conv_id) if conv_id else []
    if latest_user_msg and (not server_history or server_history[-1] != latest_user_msg):
        server_history.append(latest_user_msg)

    messages = [system_prompt] + server_history

    # 第一轮非流式，拿完整 tool_calls
    try:
        first_data = call_llm_sync(messages, tools=tools_schema)
    except RuntimeError as e:
        if want_stream:
            return Response(make_error_stream(str(e)), content_type='text/event-stream; charset=utf-8')
        return jsonify({"error": str(e)}), 502

    choice = safe_get_choice(first_data)

    # ---- 有工具调用 ----
    if choice.get("tool_calls"):
        # 把 assistant 的 tool_calls 消息存入历史
        messages.append(choice)
        tool_results = execute_all_tool_calls(choice["tool_calls"])
        messages.extend(tool_results)

        try:
            if want_stream:
                # 第二轮流式，工具结果已在 messages 里
                stream_resp = call_llm_stream(messages)

                def _stream_and_save():
                    """透传流式，同时收集 assistant 回复内容用于存历史。"""
                    collected = []
                    buf = b""
                    try:
                        for chunk in stream_resp.iter_content(chunk_size=4096):
                            if not chunk:
                                continue
                            yield chunk
                            # 在缓冲区里解析完整的 SSE 行，收集 delta content
                            buf += chunk
                            while b"\n" in buf:
                                line_bytes, buf = buf.split(b"\n", 1)
                                line = line_bytes.decode("utf-8", errors="replace").strip()
                                if line.startswith("data:") and "[DONE]" not in line:
                                    try:
                                        chunk_data = json.loads(line[5:].strip())
                                        delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                                        if delta.get("content"):
                                            collected.append(delta["content"])
                                    except Exception:
                                        pass
                    except Exception as e:
                        yield _make_sse_chunk(content=f"[流式传输中断: {str(e)}]", finish_reason="stop")
                        yield b"data: [DONE]\n\n"
                    finally:
                        stream_resp.close()
                        # 把完整的 assistant 回复存入 session 历史
                        if conv_id:
                            final_content = "".join(collected)
                            final_messages = messages + [{"role": "assistant", "content": final_content}]
                            _save_session(conv_id, final_messages)

                return Response(_stream_and_save(), content_type='text/event-stream; charset=utf-8')

            else:
                second_data = call_llm_sync(messages)
                # 存历史
                if conv_id:
                    assistant_msg = safe_get_choice(second_data)
                    final_messages = messages + [assistant_msg] if assistant_msg else messages
                    _save_session(conv_id, final_messages)
                return Response(
                    json.dumps(second_data, ensure_ascii=False),
                    content_type='application/json; charset=utf-8'
                )
        except RuntimeError as e:
            if want_stream:
                return Response(make_error_stream(str(e)), content_type='text/event-stream; charset=utf-8')
            return jsonify({"error": str(e)}), 502

    # ---- 无工具调用 ----
    # 存历史
    if conv_id:
        final_messages = messages + [choice] if choice else messages
        _save_session(conv_id, final_messages)

    if want_stream:
        return Response(wrap_as_stream(first_data), content_type='text/event-stream; charset=utf-8')

    return Response(
        json.dumps(first_data, ensure_ascii=False),
        content_type='application/json; charset=utf-8'
    )


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
    app.run(host='127.0.0.1', port=5846, debug=False, threaded=True)

import os
import glob
import json
import shutil
import subprocess
import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ==== 1. 基础配置 ====
API_KEY = "sk-9b7a62c8cee0aa73203584c7f2d39d1e0adb507dd7c90b8f34f9bbf073a966f7"
API_BASE = "https://api.lmuai.com"
API_URL = f"{API_BASE}/v1/chat/completions"
MODEL_NAME = "claude-sonnet-4-6"

DOWNLOAD_DIR = os.path.expanduser("~")

# 工具输出最大长度，超出截断，避免撑爆 context
TOOL_OUTPUT_MAX_CHARS = 8000


# ==== 2. 工具函数 ====

def read_phone_file(filename):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        size = os.path.getsize(path)
        if size > 500 * 1024:  # 超过 500KB 拒绝读取
            return f"错误: 文件 {filename} 过大 ({size // 1024}KB)，请指定更小的文件"
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"错误: 无法读取文件 {filename}。原因: {str(e)}"


def write_phone_file(filename, content):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"成功: 已保存至 {path}"
    except Exception as e:
        return f"错误: 无法写入文件。原因: {str(e)}"


def execute_local_command(command=None, **kwargs):
    # 兼容 AI 可能传来的其他参数名
    if command is None:
        command = kwargs.get("cmd") or kwargs.get("shell_command") or kwargs.get("shell") or ""
    if not command:
        return "错误: 未提供命令"
    try:
        result = subprocess.run(
            command, shell=True, text=True, capture_output=True, timeout=30
        )
        output = f"【标准输出】:\n{result.stdout}\n【标准错误】:\n{result.stderr}"
        # 截断过长输出
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
            "description": "在手机 Termux home 目录写入或创建文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "保存的文件名，例如 summary.txt"},
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


# ==== 3. 辅助函数 ====

def make_headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def safe_get_choice(data):
    """安全获取 choices[0].message，返回 dict 或空 dict。"""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list) or len(choices) == 0:
        return {}
    return choices[0].get("message") or {}


def call_llm_sync(messages, tools=None):
    """非流式请求，返回解析后的 dict。"""
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,  # 明确禁用流式，防止上游默认开启
    }
    if tools:
        payload["tools"] = tools
    try:
        resp = requests.post(API_URL, json=payload, headers=make_headers(), timeout=120)
        resp.raise_for_status()
        return json.loads(resp.content.decode("utf-8"))
    except requests.exceptions.Timeout:
        raise RuntimeError("上游 LLM 请求超时")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"上游 LLM 返回错误: {e.response.status_code} {e.response.text[:200]}")
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        raise RuntimeError(f"请求或解析失败: {str(e)}")


def call_llm_stream(messages, tools=None):
    """流式请求，返回 requests.Response 对象（未读取 body）。"""
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    try:
        resp = requests.post(
            API_URL, json=payload, headers=make_headers(),
            stream=True, timeout=120
        )
        resp.raise_for_status()
        return resp
    except requests.exceptions.Timeout:
        raise RuntimeError("上游 LLM 请求超时")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"上游 LLM 返回错误: {e.response.status_code} {e.response.text[:200]}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求失败: {str(e)}")


def execute_all_tool_calls(tool_calls):
    """执行所有 tool_calls，返回 tool 消息列表。"""
    results = []
    for tool_call in tool_calls:
        # 安全提取字段
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

        # 截断过长的工具输出
        result_str = str(result)
        if len(result_str) > TOOL_OUTPUT_MAX_CHARS:
            result_str = result_str[:TOOL_OUTPUT_MAX_CHARS] + f"\n...[已截断]"

        results.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": func_name,
            "content": result_str,
        })
    return results


def stream_proxy(upstream_resp):
    """将上游 SSE 流逐块透传，捕获 iter_content 中的异常。"""
    try:
        for chunk in upstream_resp.iter_content(chunk_size=None):
            if chunk:
                yield chunk
    except Exception as e:
        # 流中途断了，发一个错误 chunk 告知客户端
        err = f"[流式传输中断: {str(e)}]"
        error_chunk = {
            "choices": [{"delta": {"content": err}, "finish_reason": "stop", "index": 0}]
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
    finally:
        upstream_resp.close()


def make_error_stream(message):
    """把错误信息包装成合法的 SSE chunk 序列。"""
    import time
    err_id = f"err-{int(time.time())}"
    chunk = {
        "id": err_id,
        "object": "chat.completion.chunk",
        "model": MODEL_NAME,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": f"[错误] {message}"},
            "finish_reason": None,
        }]
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    end_chunk = {
        "id": err_id,
        "object": "chat.completion.chunk",
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    }
    yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def wrap_as_stream(data):
    """把非流式响应 dict 包装成合法的 SSE chunk 序列，不重复请求。"""
    resp_id = data.get("id", "")
    choices = data.get("choices", [])
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    finish_reason = choices[0].get("finish_reason", "stop") if choices else "stop"

    # content 可能为 None（某些模型在 tool_calls 时不返回 content）
    if content is None:
        content = ""

    chunk = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

    end_chunk = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
    }
    yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


# ==== 4. 路由 ====

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    chatbox_data = request.json or {}
    user_messages = chatbox_data.get("messages", [])
    want_stream = chatbox_data.get("stream", False)

    # 过滤掉 Chatbox 可能已经注入的 system message，避免重复
    user_messages = [m for m in user_messages if m.get("role") != "system"]

    system_prompt = {
        "role": "system",
        "content": (
            f"你是一个运行在安卓手机 Termux 里的多功能 AI 助手。"
            f"你的工作目录是 {DOWNLOAD_DIR}。"
            f"需要操作文件或执行命令时，请调用对应工具；"
            f"普通对话直接回复即可，不要无谓地调用工具。"
        )
    }
    messages = [system_prompt] + user_messages

    # 第一轮必须非流式，才能完整拿到 tool_calls
    try:
        first_data = call_llm_sync(messages, tools=tools_schema)
    except RuntimeError as e:
        if want_stream:
            return Response(make_error_stream(str(e)), content_type='text/event-stream; charset=utf-8')
        return jsonify({"error": str(e)}), 502

    choice = safe_get_choice(first_data)

    # ---- 有工具调用 ----
    if choice.get("tool_calls"):
        messages.append(choice)
        tool_results = execute_all_tool_calls(choice["tool_calls"])
        messages.extend(tool_results)

        try:
            if want_stream:
                stream_resp = call_llm_stream(messages)
                return Response(
                    stream_proxy(stream_resp),
                    content_type='text/event-stream; charset=utf-8'
                )
            else:
                second_data = call_llm_sync(messages)
                return Response(
                    json.dumps(second_data, ensure_ascii=False),
                    content_type='application/json; charset=utf-8'
                )
        except RuntimeError as e:
            if want_stream:
                return Response(make_error_stream(str(e)), content_type='text/event-stream; charset=utf-8')
            return jsonify({"error": str(e)}), 502

    # ---- 无工具调用 ----
    if want_stream:
        # 第一轮已有结果，直接包装成 SSE，不重复请求
        return Response(wrap_as_stream(first_data), content_type='text/event-stream; charset=utf-8')

    return Response(
        json.dumps(first_data, ensure_ascii=False),
        content_type='application/json; charset=utf-8'
    )


if __name__ == '__main__':
    # threaded=True 让每个请求在独立线程处理，避免工具执行阻塞后续请求
    app.run(host='127.0.0.1', port=5846, debug=False, threaded=True)

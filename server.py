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


# ==== 2. 定义 Agent 的核心工具（Tools） ====

def read_phone_file(filename):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"错误: 无法读取文件 {filename}。原因: {str(e)}"


def write_phone_file(filename, content):
    path = os.path.join(DOWNLOAD_DIR, filename)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"成功: 已保存至手机 Download/{filename}"
    except Exception as e:
        return f"错误: 无法写入文件。原因: {str(e)}"


def execute_local_command(command=None, **kwargs):
    # 兼容 AI 可能传来的其他参数名
    if command is None:
        command = kwargs.get("cmd") or kwargs.get("shell_command") or kwargs.get("shell") or ""
    if not command:
        return "错误: 未提供命令"
    try:
        result = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=30)
        return f"【标准输出】:\n{result.stdout}\n【标准错误】:\n{result.stderr}"
    except Exception as e:
        return f"错误: 命令执行失败。原因: {str(e)}"


def list_phone_dir(sub_dir=""):
    """List all files and folders in a specific subdirectory of Download."""
    target_dir = os.path.join(DOWNLOAD_DIR, sub_dir)
    try:
        if not os.path.exists(target_dir):
            return f"Error: Directory Download/{sub_dir} does not exist."
        items = os.listdir(target_dir)
        return json.dumps({"directory": f"Download/{sub_dir}", "contents": items}, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error listing directory: {str(e)}"


def search_phone_files(query_pattern):
    """Search for files in the Download directory using wildcard patterns (e.g., '*.pdf', 'invoice*')."""
    try:
        search_path = os.path.join(DOWNLOAD_DIR, "**", query_pattern)
        results = glob.glob(search_path, recursive=True)
        relative_results = [os.path.relpath(p, DOWNLOAD_DIR) for p in results]
        return json.dumps({"found_files": relative_results}, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error searching files: {str(e)}"


def get_phone_system_status():
    """Get Termux/Phone system metrics like battery level, storage space, and memory usage."""
    status = {}
    try:
        total, used, free = shutil.disk_usage(DOWNLOAD_DIR)
        status["storage_free_gb"] = f"{free / (1024**3):.2f} GB remaining"

        mem_info = subprocess.run("free -m", shell=True, text=True, capture_output=True).stdout
        status["memory_info_mb"] = mem_info

        battery_info = subprocess.run("termux-battery-status", shell=True, text=True, capture_output=True)
        if battery_info.returncode == 0:
            status["battery"] = json.loads(battery_info.stdout)
        else:
            status["battery"] = "Termux-API not installed; cannot fetch precise battery info."

        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error fetching system status: {str(e)}"


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
            "description": "读取手机内部存储 Download 目录下的文本、CSV 或代码文件",
            "parameters": {
                "type": "object",
                "properties": {"filename": {"type": "string", "description": "文件名，例如 data.csv"}},
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_phone_file",
            "description": "在手机内部存储 Download 目录生成、写入新文件或报告",
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
            "description": "在手机 Linux(Termux) 环境内执行 shell 命令，如 python 脚本、pip、查看目录等",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_phone_dir",
            "description": "List all files and subfolders inside the phone's Download directory or a specific subfolder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sub_dir": {"type": "string", "description": "Optional subfolder name inside Download, e.g., 'Documents'. Leave empty for root."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_phone_files",
            "description": "Search for specific files in the phone storage using wildcards, extensions, or keywords.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_pattern": {"type": "string", "description": "The search pattern, e.g., '*.pdf', '*invoice*', or 'data.csv'"}
                },
                "required": ["query_pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_phone_system_status",
            "description": "Fetch the phone's current storage space availability, Termux RAM usage, and battery life status.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]


# ==== 3. 辅助函数 ====

def make_headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def call_llm(messages, tools=None):
    """向上游 LLM 发起请求，返回解析后的 dict。"""
    payload = {"model": MODEL_NAME, "messages": messages}
    if tools:
        payload["tools"] = tools
    try:
        resp = requests.post(
            API_URL,
            json=payload,
            headers=make_headers(),
            timeout=120
        )
        resp.raise_for_status()
        # 强制用 UTF-8 解码原始字节，避免 requests 猜错编码
        return json.loads(resp.content.decode("utf-8"))
    except requests.exceptions.Timeout:
        raise RuntimeError("上游 LLM 请求超时")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"上游 LLM 返回错误: {e.response.status_code} {e.response.text}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求失败: {str(e)}")


def execute_all_tool_calls(tool_calls):
    """执行模型返回的所有 tool_calls，返回 tool 消息列表。"""
    results = []
    for tool_call in tool_calls:
        func_name = tool_call["function"]["name"]
        try:
            func_args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError:
            func_args = {}

        if func_name in tools_map:
            try:
                result = tools_map[func_name](**func_args)
            except TypeError as e:
                result = f"错误: 工具参数不匹配 ({str(e)})，收到的参数: {func_args}"
        else:
            result = f"错误: 未知工具 {func_name}"

        results.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "name": func_name,
            "content": str(result)
        })
    return results


# ==== 4. 接收并包装 Chatbox 请求的路由 ====

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    chatbox_data = request.json or {}
    user_messages = chatbox_data.get("messages", [])

    system_prompt = {
        "role": "system",
        "content": (
            f"你是一个运行在安卓手机 Termux 里的多功能 AI 助手。"
            f"你可以直接读写手机 Download 文件夹 ({DOWNLOAD_DIR})。"
            f"请通过调用工具来满足用户的需求。"
        )
    }
    messages = [system_prompt] + user_messages

    try:
        # 第一轮：判断是否需要调用工具
        first_data = call_llm(messages, tools=tools_schema)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    choice = first_data.get("choices", [{}])[0].get("message", {})

    # ---- 有工具调用：执行所有工具，再发第二轮请求 ----
    if choice.get("tool_calls"):
        messages.append(choice)
        tool_results = execute_all_tool_calls(choice["tool_calls"])
        messages.extend(tool_results)

        try:
            second_data = call_llm(messages)
            return Response(
                json.dumps(second_data, ensure_ascii=False),
                content_type='application/json; charset=utf-8'
            )
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 502

    # ---- 无工具调用，直接返回 ----
    return Response(
        json.dumps(first_data, ensure_ascii=False),
        content_type='application/json; charset=utf-8'
    )


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5846, debug=False)

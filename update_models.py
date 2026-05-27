#!/usr/bin/env python3
"""
update_models.py - Termux Agent Server 模型配置管理工具

用法：
  python update_models.py              # 进入交互菜单
  python update_models.py list         # 列出所有模型
  python update_models.py add-provider # 交互式添加 Provider
  python update_models.py add-model    # 交互式添加模型
  python update_models.py remove       # 交互式删除 Provider 或模型
  python update_models.py test         # 测试 API 连接
  python update_models.py set-default  # 设置默认模型
"""

import os
import sys
import json
import time
import requests

# 配置文件路径（与 server_stream.py 同目录）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_config.json")

# 默认空配置结构
DEFAULT_CONFIG = {
    "providers": {},
    "default_provider": "",
    "default_model": ""
}


# ==== 配置文件读写 ====

def load_config() -> dict:
    """读取配置文件，不存在时返回空配置。"""
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[错误] 读取配置文件失败: {e}")
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    """
    保存配置文件，同时打印完整内容给用户看。
    写入和打印同步进行，确保用户看到的就是写入的内容。
    """
    content = json.dumps(cfg, ensure_ascii=False, indent=2)

    # 先写入文件
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    # 再打印给用户看
    print()
    print("=" * 60)
    print(f"已写入 {CONFIG_PATH}：")
    print("=" * 60)
    print(content)
    print("=" * 60)


# ==== 格式化输出 ====

def print_config(cfg: dict):
    """格式化打印当前所有 Provider 和模型。"""
    providers = cfg.get("providers", {})
    default_model = cfg.get("default_model", "")
    default_provider = cfg.get("default_provider", "")

    if not providers:
        print("（暂无配置，请先添加 Provider）")
        return

    total_models = 0
    for pid, provider in providers.items():
        marker = " [默认]" if pid == default_provider else ""
        print(f"\nProvider: {provider.get('name', pid)} ({provider.get('api_base', '')}){marker}")
        print(f"  Provider ID : {pid}")
        print(f"  API Key     : {_mask_key(provider.get('api_key', ''))}")
        models = provider.get("models", [])
        if not models:
            print("  （暂无模型）")
        for m in models:
            default_mark = " ← 默认" if m["id"] == default_model else ""
            tool_mark = "[支持工具]" if m.get("supports_tools", True) else "[不支持工具]"
            print(f"  ✓ {m['id']}  {m.get('name', '')}  {tool_mark}{default_mark}")
            total_models += 1

    print(f"\n总计: {len(providers)} 个 Provider, {total_models} 个模型")
    if default_model:
        print(f"默认模型: {default_model}")


def _mask_key(key: str) -> str:
    """遮盖 API Key 中间部分，只显示首尾各 6 位。"""
    if not key or len(key) <= 12:
        return key or "（未设置）"
    return key[:6] + "..." + key[-6:]


# ==== 交互辅助 ====

def prompt(text: str, default: str = "") -> str:
    """带默认值的输入提示。"""
    if default:
        val = input(f"{text} [{default}]: ").strip()
        return val if val else default
    return input(f"{text}: ").strip()


def prompt_bool(text: str, default: bool = True) -> bool:
    """是/否提示。"""
    hint = "[Y/n]" if default else "[y/N]"
    val = input(f"{text} {hint}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "是")


def choose(options: list, prompt_text: str = "请选择") -> int:
    """
    显示编号列表，返回用户选择的 0-based 索引。
    返回 -1 表示取消。
    """
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input(f"{prompt_text} [1-{len(options)}, 0=取消]: ").strip()
        if val == "0":
            return -1
        if val.isdigit() and 1 <= int(val) <= len(options):
            return int(val) - 1
        print("  输入无效，请重试")


# ==== 功能函数 ====

def cmd_list():
    """列出所有 Provider 和模型。"""
    cfg = load_config()
    print("\n=== 当前配置的模型 ===")
    print_config(cfg)
    print()


def cmd_add_provider():
    """交互式添加新的 Provider。"""
    cfg = load_config()
    print("\n=== 添加新的 API Provider ===\n")

    pid = prompt("Provider ID（如 openai、anthropic、lmuai）")
    if not pid:
        print("已取消")
        return
    if pid in cfg["providers"]:
        print(f"[警告] Provider '{pid}' 已存在，将覆盖现有配置")
        if not prompt_bool("确认覆盖？", default=False):
            print("已取消")
            return

    name = prompt("Provider 显示名称", default=pid)
    api_base = prompt("API Base URL（如 https://api.openai.com）")
    if not api_base:
        print("已取消")
        return
    api_base = api_base.rstrip("/")

    api_key = prompt("API Key（输入后会遮盖显示）")
    if not api_key:
        print("已取消")
        return

    cfg["providers"][pid] = {
        "name": name,
        "api_base": api_base,
        "api_key": api_key,
        "models": []
    }

    # 如果是第一个 Provider，自动设为默认
    if len(cfg["providers"]) == 1:
        cfg["default_provider"] = pid

    print(f"\n✓ Provider '{name}' 已添加（API Key: {_mask_key(api_key)}）")

    # 询问是否立即添加模型
    if prompt_bool("\n是否立即添加模型到此 Provider？"):
        _add_model_to_provider(cfg, pid)
    else:
        save_config(cfg)


def cmd_add_model():
    """交互式添加模型到已有 Provider。"""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("[错误] 没有可用的 Provider，请先添加 Provider")
        return

    print("\n=== 添加模型 ===\n")
    print("选择目标 Provider：")
    pids = list(providers.keys())
    labels = [f"{providers[p].get('name', p)} ({p})" for p in pids]
    idx = choose(labels)
    if idx == -1:
        print("已取消")
        return

    _add_model_to_provider(cfg, pids[idx])


def _add_model_to_provider(cfg: dict, pid: str):
    """向指定 Provider 添加一个或多个模型（内部函数）。"""
    provider = cfg["providers"][pid]
    existing_ids = {m["id"] for m in provider.get("models", [])}

    while True:
        print(f"\n--- 向 {provider.get('name', pid)} 添加模型 ---")
        model_id = prompt("模型 ID（如 gpt-4o、claude-3-5-sonnet-20241022）")
        if not model_id:
            print("已取消")
            break

        if model_id in existing_ids:
            print(f"[警告] 模型 '{model_id}' 已存在")
            if not prompt_bool("覆盖？", default=False):
                continue

        model_name = prompt("模型显示名称", default=model_id)
        supports_tools = prompt_bool("支持工具调用（function calling）？", default=True)
        max_tokens_str = prompt("最大 Token 数", default="8192")
        try:
            max_tokens = int(max_tokens_str)
        except ValueError:
            max_tokens = 8192

        new_model = {
            "id": model_id,
            "name": model_name,
            "supports_tools": supports_tools,
            "max_tokens": max_tokens
        }

        # 替换或追加
        models = provider.get("models", [])
        replaced = False
        for i, m in enumerate(models):
            if m["id"] == model_id:
                models[i] = new_model
                replaced = True
                break
        if not replaced:
            models.append(new_model)
        provider["models"] = models
        existing_ids.add(model_id)

        # 如果还没有默认模型，自动设置
        if not cfg.get("default_model"):
            cfg["default_model"] = model_id
            cfg["default_provider"] = pid
            print(f"✓ 已自动设置 '{model_id}' 为默认模型")

        print(f"✓ 模型 '{model_id}' 已添加")

        if not prompt_bool("\n继续添加更多模型？", default=False):
            break

    save_config(cfg)


def cmd_remove():
    """交互式删除 Provider 或模型。"""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("没有可删除的配置")
        return

    print("\n=== 删除配置 ===\n")
    action = choose(["删除某个模型", "删除整个 Provider"], "选择操作")
    if action == -1:
        print("已取消")
        return

    pids = list(providers.keys())
    labels = [f"{providers[p].get('name', p)} ({p})" for p in pids]
    print("\n选择 Provider：")
    pidx = choose(labels)
    if pidx == -1:
        print("已取消")
        return
    pid = pids[pidx]

    if action == 1:
        # 删除整个 Provider
        if not prompt_bool(f"确认删除 Provider '{providers[pid].get('name', pid)}' 及其所有模型？", default=False):
            print("已取消")
            return
        del cfg["providers"][pid]
        if cfg.get("default_provider") == pid:
            cfg["default_provider"] = next(iter(cfg["providers"]), "")
            cfg["default_model"] = ""
        print(f"✓ Provider '{pid}' 已删除")
        save_config(cfg)
        return

    # 删除某个模型
    models = providers[pid].get("models", [])
    if not models:
        print("该 Provider 下没有模型")
        return
    print("\n选择要删除的模型：")
    midx = choose([f"{m['id']}  {m.get('name', '')}" for m in models])
    if midx == -1:
        print("已取消")
        return
    removed = models.pop(midx)
    providers[pid]["models"] = models
    if cfg.get("default_model") == removed["id"]:
        # 重置默认模型
        cfg["default_model"] = models[0]["id"] if models else ""
    print(f"✓ 模型 '{removed['id']}' 已删除")
    save_config(cfg)


def cmd_test():
    """测试指定 Provider + 模型的 API 连接。"""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("没有可测试的 Provider")
        return

    print("\n=== 测试 API 连接 ===\n")
    pids = list(providers.keys())
    labels = [f"{providers[p].get('name', p)} ({p})" for p in pids]
    print("选择 Provider：")
    pidx = choose(labels)
    if pidx == -1:
        print("已取消")
        return
    pid = pids[pidx]
    provider = providers[pid]

    models = provider.get("models", [])
    if not models:
        print("该 Provider 下没有模型，无法测试")
        return

    print("\n选择模型：")
    midx = choose([f"{m['id']}  {m.get('name', '')}" for m in models])
    if midx == -1:
        print("已取消")
        return
    model = models[midx]

    api_url = f"{provider['api_base']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model["id"],
        "messages": [{"role": "user", "content": "Hello! Reply with exactly: OK"}],
        "stream": False,
        "max_tokens": 20
    }

    print(f"\n正在测试 {provider.get('name')} / {model['id']} ...")
    print(f"  URL: {api_url}")
    t0 = time.time()
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=30)
        elapsed = time.time() - t0
        if resp.status_code == 200:
            data = resp.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "（无内容）")
            print(f"\n✓ 连接成功！")
            print(f"  响应时间: {elapsed:.2f}s")
            print(f"  模型回复: {reply}")
        else:
            print(f"\n✗ 连接失败！HTTP {resp.status_code}")
            print(f"  响应: {resp.text[:300]}")
    except requests.exceptions.Timeout:
        print(f"\n✗ 连接超时（30s）")
    except Exception as e:
        print(f"\n✗ 连接失败: {e}")


def cmd_set_default():
    """设置默认模型。"""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("没有可用的模型")
        return

    print("\n=== 设置默认模型 ===\n")
    # 展开所有模型
    all_models = []
    for pid, provider in providers.items():
        for m in provider.get("models", []):
            all_models.append((pid, provider.get("name", pid), m))

    if not all_models:
        print("没有可用的模型")
        return

    labels = [f"{pname} / {m['id']}  {m.get('name', '')}" for pid, pname, m in all_models]
    print("选择默认模型：")
    idx = choose(labels)
    if idx == -1:
        print("已取消")
        return

    pid, _, model = all_models[idx]
    cfg["default_provider"] = pid
    cfg["default_model"] = model["id"]
    print(f"\n✓ 默认模型已设置为: {model['id']}")
    save_config(cfg)


# ==== 交互菜单 ====

def interactive_menu():
    """主交互菜单（无命令行参数时进入）。"""
    print("\n=== Termux Agent Server - 模型管理 ===")
    print(f"配置文件: {CONFIG_PATH}")

    actions = [
        ("list",        "列出所有模型",          cmd_list),
        ("add-provider","添加新的 Provider",      cmd_add_provider),
        ("add-model",   "添加模型到现有 Provider", cmd_add_model),
        ("remove",      "删除 Provider 或模型",   cmd_remove),
        ("test",        "测试 API 连接",          cmd_test),
        ("set-default", "设置默认模型",           cmd_set_default),
    ]

    while True:
        print()
        for i, (_, label, _) in enumerate(actions, 1):
            print(f"  {i}. {label}")
        print("  0. 退出")
        print()
        val = input("请选择操作 [0-6]: ").strip()
        if val == "0" or val.lower() in ("q", "quit", "exit"):
            print("再见！")
            break
        if val.isdigit() and 1 <= int(val) <= len(actions):
            actions[int(val) - 1][2]()
        else:
            print("输入无效，请重试")


# ==== 入口 ====

COMMANDS = {
    "list":         cmd_list,
    "add-provider": cmd_add_provider,
    "add-model":    cmd_add_model,
    "remove":       cmd_remove,
    "test":         cmd_test,
    "set-default":  cmd_set_default,
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # 无参数：进入交互菜单
        interactive_menu()
    else:
        cmd = sys.argv[1].lower()
        if cmd in COMMANDS:
            COMMANDS[cmd]()
        elif cmd in ("-h", "--help", "help"):
            print(__doc__)
        else:
            print(f"未知命令: {cmd}")
            print(f"可用命令: {', '.join(COMMANDS.keys())}")
            print("运行 python update_models.py --help 查看帮助")
            sys.exit(1)

#!/usr/bin/env python3
"""
update_models.py - AIPhoneTools Model Configuration Manager

Usage:
  python update_models.py              # Enter interactive menu
  python update_models.py list         # List all models
  python update_models.py add-provider # Interactively add a Provider
  python update_models.py add-model    # Interactively add a model
  python update_models.py remove       # Interactively remove Provider or model
  python update_models.py test         # Test API connection
  python update_models.py set-default  # Set default model
"""

import os
import sys
import json
import time
import requests

# Config file path (same directory as server.py)
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_config.json")

# Default empty config structure
DEFAULT_CONFIG = {
    "providers": {},
    "default_provider": "",
    "default_model": ""
}


# ==== Config File I/O ====

def load_config() -> dict:
    """Load config file, return empty config if not exists."""
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to read config file: {e}")
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    """
    Save config file and print full content to user.
    Write and print synchronously to ensure user sees what was written.
    """
    content = json.dumps(cfg, ensure_ascii=False, indent=2)

    # Write to file first
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    # Then print to user
    print()
    print("=" * 60)
    print(f"Written to {CONFIG_PATH}:")
    print("=" * 60)
    print(content)
    print("=" * 60)


# ==== Formatted Output ====

def print_config(cfg: dict):
    """Pretty-print all Providers and models."""
    providers = cfg.get("providers", {})
    default_model = cfg.get("default_model", "")
    default_provider = cfg.get("default_provider", "")

    if not providers:
        print("(No configuration yet, please add a Provider first)")
        return

    total_models = 0
    for pid, provider in providers.items():
        marker = " [default]" if pid == default_provider else ""
        print(f"\nProvider: {provider.get('name', pid)} ({provider.get('api_base', '')}){marker}")
        print(f"  Provider ID : {pid}")
        print(f"  API Key     : {_mask_key(provider.get('api_key', ''))}")
        models = provider.get("models", [])
        if not models:
            print("  (No models yet)")
        for m in models:
            default_mark = " <- default" if m["id"] == default_model else ""
            tool_mark = "[tools]" if m.get("supports_tools", True) else "[no tools]"
            print(f"  ✓ {m['id']}  {m.get('name', '')}  {tool_mark}{default_mark}")
            total_models += 1

    print(f"\nTotal: {len(providers)} Provider(s), {total_models} model(s)")
    if default_model:
        print(f"Default model: {default_model}")


def _mask_key(key: str) -> str:
    """Mask middle part of API Key, show only first and last 6 chars."""
    if not key or len(key) <= 12:
        return key or "(not set)"
    return key[:6] + "..." + key[-6:]


# ==== Interactive Helpers ====

def prompt(text: str, default: str = "") -> str:
    """Input prompt with default value."""
    if default:
        val = input(f"{text} [{default}]: ").strip()
        return val if val else default
    return input(f"{text}: ").strip()


def prompt_bool(text: str, default: bool = True) -> bool:
    """Yes/No prompt."""
    hint = "[Y/n]" if default else "[y/N]"
    val = input(f"{text} {hint}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def choose(options: list, prompt_text: str = "Choose") -> int:
    """
    Display numbered list, return user's 0-based index choice.
    Returns -1 for cancel.
    """
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input(f"{prompt_text} [1-{len(options)}, 0=cancel]: ").strip()
        if val == "0":
            return -1
        if val.isdigit() and 1 <= int(val) <= len(options):
            return int(val) - 1
        print("  Invalid input, please try again")


# ==== Command Functions ====

def cmd_list():
    """List all Providers and models."""
    cfg = load_config()
    print("\n=== Current Model Configuration ===")
    print_config(cfg)
    print()


def cmd_add_provider():
    """Interactively add a new Provider."""
    cfg = load_config()
    print("\n=== Add New API Provider ===\n")

    pid = prompt("Provider ID (e.g. openai, anthropic, deepseek)")
    if not pid:
        print("Cancelled")
        return
    if pid in cfg["providers"]:
        print(f"[WARNING] Provider '{pid}' already exists, will overwrite")
        if not prompt_bool("Confirm overwrite?", default=False):
            print("Cancelled")
            return

    name = prompt("Provider display name", default=pid)
    api_base = prompt("API Base URL (e.g. https://api.openai.com)")
    if not api_base:
        print("Cancelled")
        return
    api_base = api_base.rstrip("/")

    api_key = prompt("API Key (will be masked after input)")
    if not api_key:
        print("Cancelled")
        return

    cfg["providers"][pid] = {
        "name": name,
        "api_base": api_base,
        "api_key": api_key,
        "models": []
    }

    # If first Provider, auto-set as default
    if len(cfg["providers"]) == 1:
        cfg["default_provider"] = pid

    print(f"\n✓ Provider '{name}' added (API Key: {_mask_key(api_key)})")

    # Ask if user wants to add models immediately
    if prompt_bool("\nAdd models to this Provider now?"):
        _add_model_to_provider(cfg, pid)
    else:
        save_config(cfg)


def cmd_add_model():
    """Interactively add model to existing Provider."""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("[ERROR] No Providers available, please add a Provider first")
        return

    print("\n=== Add Model ===\n")
    print("Select target Provider:")
    pids = list(providers.keys())
    labels = [f"{providers[p].get('name', p)} ({p})" for p in pids]
    idx = choose(labels)
    if idx == -1:
        print("Cancelled")
        return

    _add_model_to_provider(cfg, pids[idx])


def _add_model_to_provider(cfg: dict, pid: str):
    """Add one or more models to specified Provider (internal function)."""
    provider = cfg["providers"][pid]
    existing_ids = {m["id"] for m in provider.get("models", [])}

    while True:
        print(f"\n--- Add model to {provider.get('name', pid)} ---")
        model_id = prompt("Model ID (e.g. gpt-4o, claude-3-5-sonnet-20241022)")
        if not model_id:
            print("Cancelled")
            break

        if model_id in existing_ids:
            print(f"[WARNING] Model '{model_id}' already exists")
            if not prompt_bool("Overwrite?", default=False):
                continue

        model_name = prompt("Model display name", default=model_id)
        supports_tools = prompt_bool("Supports tool calling (function calling)?", default=True)
        max_tokens_str = prompt("Max tokens", default="8192")
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

        # Replace or append
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

        # If no default model yet, auto-set
        if not cfg.get("default_model"):
            cfg["default_model"] = model_id
            cfg["default_provider"] = pid
            print(f"✓ Auto-set '{model_id}' as default model")

        print(f"✓ Model '{model_id}' added")

        if not prompt_bool("\nAdd more models?", default=False):
            break

    save_config(cfg)


def cmd_remove():
    """Interactively remove Provider or model."""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("No configuration to remove")
        return

    print("\n=== Remove Configuration ===\n")
    action = choose(["Remove a model", "Remove entire Provider"], "Select action")
    if action == -1:
        print("Cancelled")
        return

    pids = list(providers.keys())
    labels = [f"{providers[p].get('name', p)} ({p})" for p in pids]
    print("\nSelect Provider:")
    pidx = choose(labels)
    if pidx == -1:
        print("Cancelled")
        return
    pid = pids[pidx]

    if action == 1:
        # Remove entire Provider
        if not prompt_bool(f"Confirm removal of Provider '{providers[pid].get('name', pid)}' and all its models?", default=False):
            print("Cancelled")
            return
        del cfg["providers"][pid]
        if cfg.get("default_provider") == pid:
            cfg["default_provider"] = next(iter(cfg["providers"]), "")
            cfg["default_model"] = ""
        print(f"✓ Provider '{pid}' removed")
        save_config(cfg)
        return

    # Remove a model
    models = providers[pid].get("models", [])
    if not models:
        print("This Provider has no models")
        return
    print("\nSelect model to remove:")
    midx = choose([f"{m['id']}  {m.get('name', '')}" for m in models])
    if midx == -1:
        print("Cancelled")
        return
    removed = models.pop(midx)
    providers[pid]["models"] = models
    if cfg.get("default_model") == removed["id"]:
        # Reset default model
        cfg["default_model"] = models[0]["id"] if models else ""
    print(f"✓ Model '{removed['id']}' removed")
    save_config(cfg)


def cmd_test():
    """Test API connection for specified Provider + model."""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("No Providers to test")
        return

    print("\n=== Test API Connection ===\n")
    pids = list(providers.keys())
    labels = [f"{providers[p].get('name', p)} ({p})" for p in pids]
    print("Select Provider:")
    pidx = choose(labels)
    if pidx == -1:
        print("Cancelled")
        return
    pid = pids[pidx]
    provider = providers[pid]

    models = provider.get("models", [])
    if not models:
        print("This Provider has no models, cannot test")
        return

    print("\nSelect model:")
    midx = choose([f"{m['id']}  {m.get('name', '')}" for m in models])
    if midx == -1:
        print("Cancelled")
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

    print(f"\nTesting {provider.get('name')} / {model['id']} ...")
    print(f"  URL: {api_url}")
    t0 = time.time()
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=30)
        elapsed = time.time() - t0
        if resp.status_code == 200:
            data = resp.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "(no content)")
            print(f"\n✓ Connection successful!")
            print(f"  Response time: {elapsed:.2f}s")
            print(f"  Model reply: {reply}")
        else:
            print(f"\n✗ Connection failed! HTTP {resp.status_code}")
            print(f"  Response: {resp.text[:300]}")
    except requests.exceptions.Timeout:
        print(f"\n✗ Connection timeout (30s)")
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")


def cmd_set_default():
    """Set default model."""
    cfg = load_config()
    providers = cfg.get("providers", {})
    if not providers:
        print("No models available")
        return

    print("\n=== Set Default Model ===\n")
    # Expand all models
    all_models = []
    for pid, provider in providers.items():
        for m in provider.get("models", []):
            all_models.append((pid, provider.get("name", pid), m))

    if not all_models:
        print("No models available")
        return

    labels = [f"{pname} / {m['id']}  {m.get('name', '')}" for pid, pname, m in all_models]
    print("Select default model:")
    idx = choose(labels)
    if idx == -1:
        print("Cancelled")
        return

    pid, _, model = all_models[idx]
    cfg["default_provider"] = pid
    cfg["default_model"] = model["id"]
    print(f"\n✓ Default model set to: {model['id']}")
    save_config(cfg)


# ==== Interactive Menu ====

def interactive_menu():
    """Main interactive menu (entered when no command-line args)."""
    print("\n=== AIPhoneTools - Model Manager ===")
    print(f"Config file: {CONFIG_PATH}")

    actions = [
        ("list",        "List all models",          cmd_list),
        ("add-provider","Add new Provider",         cmd_add_provider),
        ("add-model",   "Add model to Provider",    cmd_add_model),
        ("remove",      "Remove Provider or model", cmd_remove),
        ("test",        "Test API connection",      cmd_test),
        ("set-default", "Set default model",        cmd_set_default),
    ]

    while True:
        print()
        for i, (_, label, _) in enumerate(actions, 1):
            print(f"  {i}. {label}")
        print("  0. Exit")
        print()
        val = input("Select action [0-6]: ").strip()
        if val == "0" or val.lower() in ("q", "quit", "exit"):
            print("Goodbye!")
            break
        if val.isdigit() and 1 <= int(val) <= len(actions):
            actions[int(val) - 1][2]()
        else:
            print("Invalid input, please try again")


# ==== Entry Point ====

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
        # No args: enter interactive menu
        interactive_menu()
    else:
        cmd = sys.argv[1].lower()
        if cmd in COMMANDS:
            COMMANDS[cmd]()
        elif cmd in ("-h", "--help", "help"):
            print(__doc__)
        else:
            print(f"Unknown command: {cmd}")
            print(f"Available commands: {', '.join(COMMANDS.keys())}")
            print("Run python update_models.py --help for help")
            sys.exit(1)

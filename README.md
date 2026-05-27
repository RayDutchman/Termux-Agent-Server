# Termux Agent Server

一个运行在 Android 手机 Termux 环境中的 AI Agent 服务器，作为 Chatbox App 和上游 LLM API 之间的代理，提供本地工具调用能力（文件操作、命令执行等）。

## 功能特性

- 🔧 **本地工具调用** - 支持文件读写、命令执行、目录查看等操作
- 💾 **会话记忆** - 维护对话历史，解决 Chatbox 不存储 tool_calls 导致的 AI 失忆问题
- 🌊 **流式输出** - 支持流式和非流式两种模式
- 📝 **详细日志** - 完整的请求、会话、工具调用日志
- 🤖 **多模型支持** - 通过 `models_config.json` 配置多个 Provider 和模型，Chatbox 可自由切换
- 🔌 **OpenAI 兼容** - 提供标准的 OpenAI API 接口

## 架构说明

```
Chatbox App
    ↓  POST /v1/chat/completions (stream=true/false)
Flask Server (0.0.0.0:5846)  ← 运行在 Termux
    ↓  第一轮流式请求（边收边发，同时解析 tool_calls）
上游 LLM API (api.lmuai.com, claude-sonnet-4-6)
    ↓  如果有 tool_calls：静默丢弃 tool_calls chunk，执行本地工具
本地工具（读写文件、执行命令、查目录等）
    ↓  第二轮流式请求，把工具结果带上
上游 LLM API → 最终回复透传给 Chatbox
```

## 部署教程

### 第一步：安装和配置 Termux

#### 1.1 下载安装 Termux

从以下渠道之一下载 Termux：
- [F-Droid](https://f-droid.org/packages/com.termux/) （推荐）
- [GitHub Releases](https://github.com/termux/termux-app/releases)

**注意**：不要从 Google Play 下载，版本已过期。

#### 1.2 授予存储权限

打开 Termux 后，执行以下命令授予存储访问权限：

```bash
termux-setup-storage
```

系统会弹出权限请求，点击"允许"。

#### 1.3 更新软件包

```bash
pkg update && pkg upgrade -y
```

### 第二步：安装依赖

#### 2.1 安装基础工具

```bash
# 安装 Python
pkg install python -y

# 安装 Git
pkg install git -y

# 安装 OpenSSH（可选，用于远程管理）
pkg install openssh -y
```

#### 2.2 验证安装

```bash
python --version   # 应显示 Python 3.x
git --version      # 应显示 git 版本
```

### 第三步：克隆项目

```bash
# 进入 home 目录
cd ~

# 克隆项目
git clone https://github.com/RayDutchman/Termux-Agent-Server.git

# 进入项目目录
cd Termux-Agent-Server
```

### 第四步：安装 Python 依赖

```bash
# 安装项目依赖
pip install -r requirements.txt
```

依赖包括：
- `flask` - Web 服务器框架
- `requests` - HTTP 请求库

### 第五步：配置 API Key

编辑 `server_stream.py`，修改以下配置：

```python
# 第 22 行附近
API_KEY = "your-api-key-here"  # 替换为你的 API Key
API_BASE = "https://api.lmuai.com"  # 或其他兼容 OpenAI 的 API
MODEL_NAME = "claude-sonnet-4-6"  # 或其他模型
```

### 第六步：启动服务

#### 6.1 前台运行（测试用）

```bash
python server_stream.py
```

你应该看到：
```
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5846
 * Running on http://192.168.100.xxx:5846
```

按 `Ctrl+C` 停止服务。

#### 6.2 后台运行（推荐）

```bash
nohup python server_stream.py > ~/server.log 2>&1 &
```

查看日志：
```bash
tail -f ~/server.log
```

停止服务：
```bash
pkill -f server_stream.py
```

### 第七步：配置 Chatbox App

#### 7.1 下载 Chatbox

从 [Chatbox 官网](https://chatboxai.app/) 下载并安装。

#### 7.2 添加自定义 API

1. 打开 Chatbox
2. 进入 **设置** → **AI 提供商**
3. 点击 **添加自定义 API**
4. 配置如下：

| 配置项 | 值 |
|--------|-----|
| **名称** | Termux Server |
| **API 地址** | `http://192.168.100.xxx:5846` （替换为你手机的局域网 IP） |
| **API Key** | 任意值（会被忽略） |
| **模型** | `claude-sonnet-4-6` |

#### 7.3 获取手机 IP 地址

在 Termux 中执行：
```bash
hostname -I
```

或者在手机设置中查看：**设置** → **WiFi** → **当前连接的 WiFi** → **IP 地址**

#### 7.4 测试连接

在 Chatbox 设置中点击 **检查连接**，应该显示连接成功。

#### 7.5 重要配置

**移除 tool_use capability**（如果有的话）：
- 在模型配置的 **capabilities** 里移除 `tool_use`
- 这样可以避免 Chatbox 自己的工具系统拦截 tool_calls

### 第八步：测试功能

在 Chatbox 中尝试以下命令：

1. **普通对话**：`你好，介绍一下你自己`
2. **执行命令**：`执行命令 echo hello`
3. **列出目录**：`列出当前目录的文件`
4. **读取文件**：`读取 server_stream.py 的前 5 行`
5. **写入文件**：`创建文件 test.txt，内容是 hello world`
6. **系统状态**：`获取系统状态`

## 可用工具

服务器提供以下本地工具：

| 工具名称 | 功能 | 示例 |
|---------|------|------|
| `read_phone_file` | 读取文件 | "读取 data.csv" |
| `write_phone_file` | 写入文件 | "创建文件 notes.txt，内容是..." |
| `execute_local_command` | 执行命令 | "执行命令 ls -la" |
| `list_phone_dir` | 列出目录 | "列出当前目录" |
| `search_phone_files` | 搜索文件 | "搜索所有 .py 文件" |
| `get_phone_system_status` | 系统状态 | "获取系统状态" |

## 多模型支持

### 工作原理

服务器启动时读取 `models_config.json`，其中可以配置多个 API Provider（如 LMU AI、OpenAI、Anthropic 等），每个 Provider 下可以有多个模型。

Chatbox 调用 `/v1/models` 时，服务器返回所有已配置的模型列表，用户可以在 Chatbox 的模型选择器中自由切换。每次请求时，服务器根据请求中的 `model` 字段自动路由到对应的 Provider 和 API Key。

### 配置文件结构

`models_config.json` 示例（参考 `models_config.example.json`）：

```json
{
  "providers": {
    "lmuai": {
      "name": "LMU AI",
      "api_base": "https://api.lmuai.com",
      "api_key": "sk-xxx...",
      "models": [
        {
          "id": "claude-sonnet-4-6",
          "name": "Claude Sonnet 4.6",
          "supports_tools": true,
          "max_tokens": 8192
        },
        {
          "id": "gpt-4o",
          "name": "GPT-4o",
          "supports_tools": true,
          "max_tokens": 8192
        }
      ]
    },
    "openai": {
      "name": "OpenAI",
      "api_base": "https://api.openai.com",
      "api_key": "sk-yyy...",
      "models": [
        {
          "id": "gpt-4-turbo",
          "name": "GPT-4 Turbo",
          "supports_tools": true,
          "max_tokens": 8192
        }
      ]
    }
  },
  "default_provider": "lmuai",
  "default_model": "claude-sonnet-4-6"
}
```

**注意**：`models_config.json` 已加入 `.gitignore`，不会被提交到 Git，保护 API Key 安全。首次使用请复制 `models_config.example.json` 并填入真实 Key。

### 使用 update_models.py 管理模型

`update_models.py` 是配套的模型管理脚本，支持两种使用方式：

#### 交互菜单（推荐）

```bash
python update_models.py
```

进入菜单后可以：
1. 列出所有模型
2. 添加新的 Provider
3. 添加模型到现有 Provider
4. 删除 Provider 或模型
5. 测试 API 连接
6. 设置默认模型

每次保存时，脚本会**同时写入文件并打印完整配置内容**，方便确认。

#### 命令行参数（快捷方式）

```bash
python update_models.py list          # 列出所有模型
python update_models.py add-provider  # 交互式添加 Provider
python update_models.py add-model     # 交互式添加模型
python update_models.py remove        # 删除 Provider 或模型
python update_models.py test          # 测试 API 连接
python update_models.py set-default   # 设置默认模型
```

#### 示例：添加 OpenAI Provider

```
$ python update_models.py add-provider

=== 添加新的 API Provider ===

Provider ID（如 openai、anthropic、lmuai）: openai
Provider 显示名称 [openai]: OpenAI
API Base URL（如 https://api.openai.com）: https://api.openai.com
API Key（输入后会遮盖显示）: sk-xxx...

✓ Provider 'OpenAI' 已添加（API Key: sk-xxx...xxx）

是否立即添加模型到此 Provider？ [Y/n]: y

--- 向 OpenAI 添加模型 ---
模型 ID（如 gpt-4o、claude-3-5-sonnet-20241022）: gpt-4o
模型显示名称 [gpt-4o]: GPT-4o
支持工具调用（function calling）？ [Y/n]: y
最大 Token 数 [8192]:

✓ 模型 'gpt-4o' 已添加

继续添加更多模型？ [y/N]: n

============================================================
已写入 /path/to/models_config.json：
============================================================
{
  "providers": { ... }
}
============================================================
```

### 修改配置后重启服务

`models_config.json` 在服务器启动时加载一次，修改后需要重启服务才能生效：

```bash
pkill -f server_stream.py
nohup python server_stream.py > ~/server.log 2>&1 &
```



### 服务器配置

在 `server_stream.py` 顶部可以修改以下配置：

```python
# API 配置
API_KEY = "your-api-key"
API_BASE = "https://api.lmuai.com"
MODEL_NAME = "claude-sonnet-4-6"

# 工作目录
DOWNLOAD_DIR = os.path.expanduser("~")  # Termux home 目录

# 工具输出限制
TOOL_OUTPUT_MAX_CHARS = 8000  # 工具输出最大字符数

# 会话历史限制
SESSION_MAX_TURNS = 20  # 最多保留的对话轮数
```

### 端口配置

默认监听 `0.0.0.0:5846`，如需修改：

```python
# 文件末尾
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5846, debug=False, threaded=True)
```

## 故障排查

### 问题 1：Chatbox 连接失败

**症状**：Chatbox 显示"连接失败"或"无法访问"

**解决方案**：
1. 确认服务正在运行：`ps aux | grep server_stream.py`
2. 确认手机和电脑在同一 WiFi 网络
3. 确认 IP 地址正确：`hostname -I`
4. 测试连接：`curl http://localhost:5846/v1/models`

### 问题 2：工具不执行

**症状**：AI 说"没有工具"或不执行命令

**解决方案**：
1. 查看日志：`tail -50 ~/server.log`
2. 确认日志中有 `[TOOL]` 相关信息
3. 检查 Chatbox 模型配置，移除 `tool_use` capability

### 问题 3：AI 失忆

**症状**：AI 不记得之前的对话

**解决方案**：
1. 查看日志中的 `conv_id`，确认不为空
2. 查看 `[SESSION] history_length`，确认大于 0
3. 如果 `conv_id` 为空，检查 Chatbox 是否发送了会话 ID

### 问题 4：权限错误

**症状**：无法读写文件

**解决方案**：
1. 确认已授予存储权限：`termux-setup-storage`
2. 检查文件路径是否在 Termux home 目录下
3. 查看日志中的错误信息

## 高级功能

### SSH 远程管理

如果安装了 OpenSSH，可以从电脑远程管理：

```bash
# 在 Termux 中设置密码
passwd

# 启动 SSH 服务
sshd

# 从电脑连接（默认端口 8022）
ssh -p 8022 u0_aXXX@192.168.100.xxx
```

### 自动启动

创建启动脚本 `~/start_server.sh`：

```bash
#!/data/data/com.termux/files/usr/bin/bash
cd ~/Termux-Agent-Server
nohup python server_stream.py > ~/server.log 2>&1 &
echo "Server started. Check log: tail -f ~/server.log"
```

添加执行权限：
```bash
chmod +x ~/start_server.sh
```

使用：
```bash
~/start_server.sh
```

### 查看实时日志

```bash
# 实时查看所有日志
tail -f ~/server.log

# 只看工具调用日志
tail -f ~/server.log | grep TOOL

# 只看错误日志
tail -f ~/server.log | grep -E "ERROR|WARNING"
```

## API 端点

服务器提供以下 API 端点：

### POST /v1/chat/completions

标准的 OpenAI 兼容聊天接口。

**请求示例**：
```json
{
  "model": "claude-sonnet-4-6",
  "messages": [
    {"role": "user", "content": "执行命令 echo hello"}
  ],
  "stream": false,
  "conversation_id": "optional-session-id"
}
```

### GET /v1/models

获取可用模型列表。

**响应示例**：
```json
{
  "object": "list",
  "data": [
    {
      "id": "claude-sonnet-4-6",
      "object": "model",
      "created": 1700000000,
      "owned_by": "termux-agent"
    }
  ]
}
```

### DELETE /v1/sessions

清空所有会话历史（调试用）。

### DELETE /v1/sessions/{conv_id}

清空指定会话历史。

## 开发和测试

### 运行自动化测试

```bash
python test_api.py
```

测试包括：
- 连接检测
- 普通对话
- 工具调用
- Session 记忆
- 读写文件

### 查看测试计划

详细的测试用例和验证步骤见 `TEST_PLAN.md`。

## 项目结构

```
Termux-Agent-Server/
├── server_stream.py      # 主服务器（流式支持）
├── server.py             # 旧版服务器（非流式）
├── test_api.py           # 自动化测试脚本
├── TEST_PLAN.md          # 测试计划文档
├── requirements.txt      # Python 依赖
└── README.md             # 本文档
```

## 注意事项

1. **网络要求**：手机和电脑必须在同一局域网
2. **电量消耗**：长时间运行会消耗电量，建议连接充电器
3. **后台运行**：某些 Android 系统可能会杀掉后台进程，需要在设置中允许 Termux 后台运行
4. **API Key 安全**：不要将包含真实 API Key 的代码提交到公开仓库

## 更新日志

### v1.0.0 (2026-05-26)

- ✅ 实现流式和非流式两种模式
- ✅ 支持 6 种本地工具
- ✅ Session 历史管理
- ✅ 详细的调试日志
- ✅ 修复上游 API 返回的错误 arguments 格式
- ✅ OpenAI 兼容接口

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License

## 相关链接

- [Termux 官网](https://termux.dev/)
- [Chatbox 官网](https://chatboxai.app/)
- [项目 GitHub](https://github.com/RayDutchman/Termux-Agent-Server)

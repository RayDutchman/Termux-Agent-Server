# AIPhoneTools

一个运行在 Android 手机 Termux 环境中的 AI Agent 服务器，让 AI 能够操作你的手机——读写文件、执行命令、调用 Termux-API。

## 核心特性

- **多模型支持** - 动态加载模型列表，Chatbox 可自由切换
- **本地工具执行** - 7 种工具：文件读写、命令执行、目录查看、文件搜索、系统状态、全局记忆更新
- **Termux-API 集成** - 支持 GPS、剪贴板、通知、TTS、相机等 40+ 功能
- **长期记忆** - 每次新会话的第一条消息时自动加载 `~/memory.md`（最多 8000 字符）
- **多轮工具调用** - 最多 20 轮，自动分批执行，实时显示调用进度
- **时间预算机制** - 50 秒自动中断，防止客户端 timeout
- **SSE 心跳** - 工具执行期间每 5 秒发送心跳，防止 SSE 空闲超时
- **OpenAI 兼容** - 标准 API 接口，兼容任何 OpenAI 客户端

## 快速开始

### 1. 安装 Termux

从 [Google Play](https://play.google.com/store/apps/details?id=com.termux)、[F-Droid](https://f-droid.org/packages/com.termux/) 或 [GitHub Releases](https://github.com/termux/termux-app/releases) 下载均可。

> **注意**：如需使用 Termux-API 功能（GPS、相机、短信等），还必须从 F-Droid 安装 [Termux:API App](https://f-droid.org/packages/com.termux.api/)——该 App 在 Google Play 上没有。

### 2. 安装依赖

```bash
# 更新软件包
pkg update && pkg upgrade -y

# 安装 Python 和 Git
pkg install python git -y

# 授予存储权限
termux-setup-storage

# 克隆项目
git clone https://github.com/RayDutchman/AIPhoneTools.git
cd AIPhoneTools

# 安装 Python 依赖
pip install -r requirements.txt
```

### 3. 配置 API

复制示例配置并填入你的 API Key：

```bash
cp models_config.example.json models_config.json
nano models_config.json  # 或用其他编辑器
```

配置示例：

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

### 4. 启动服务

```bash
# 前台运行（测试用）
python server.py

# 后台运行（推荐）
nohup python server.py > ~/server.log 2>&1 &

# 查看日志
tail -f ~/server.log
```

### 5. 配置 Chatbox

1. 下载 [Chatbox](https://chatboxai.app/)
2. 设置 → AI 提供商 → 添加自定义 API
3. 配置：
   - **API 地址**：`http://手机IP:5846`（用 `hostname -I` 查看 IP）
   - **API Key**：任意值
   - **模型**：从列表选择

> **建议**：在 Chatbox 对话设置中，将"上下文消息数"限制在 10 条左右，防止过长对话触发上游 token 超限错误。

## 可用工具

| 工具 | 功能 | 示例 |
|------|------|------|
| `read_phone_file` | 读取文件 | "读取 memory.md" |
| `write_phone_file` | 写入文件 | "创建 notes.txt，内容是..." |
| `execute_local_command` | 执行命令 | "执行 ls -la" |
| `list_phone_dir` | 列出目录 | "列出当前目录" |
| `search_phone_files` | 搜索文件 | "搜索所有 .py 文件" |
| `get_phone_system_status` | 系统状态 | "获取电量和内存" |
| `update_global_memory` | 更新记忆 | "记住我偏好 Python" |

### Termux-API 支持

通过 `execute_local_command` 调用 40+ Termux-API 命令：

```bash
# 在 Termux 内安装 termux-api 包
pkg install termux-api -y
```

> **必须**：还需从 F-Droid 安装 [Termux:API App](https://f-droid.org/packages/com.termux.api/)，仅安装上面的包是不够的。

常用命令：
- `termux-location` - GPS 定位
- `termux-clipboard-get/set` - 剪贴板
- `termux-notification` - 通知
- `termux-tts-speak` - 语音合成
- `termux-camera-photo` - 拍照
- `termux-sms-list/send` - 短信
- `termux-toast` - Toast 提示
- `termux-vibrate` - 震动
- `termux-torch` - 手电筒
- `termux-wifi-connectioninfo` - WiFi 信息

完整列表：`termux-api --help`

## 模型管理

使用 `update_models.py` 管理多个 Provider 和模型：

```bash
# 交互菜单
python update_models.py

# 快捷命令
python update_models.py list          # 列出所有模型
python update_models.py add-provider  # 添加 Provider
python update_models.py add-model     # 添加模型
python update_models.py test          # 测试连接
python update_models.py set-default   # 设置默认模型
```

修改配置后重启服务：

```bash
pkill -f server.py && nohup python server.py > ~/server.log 2>&1 &
```

## 高级功能

### 长期记忆

创建 `~/memory.md`，AI 在每次新会话的第一条消息时会自动加载（最多 8000 字符）。使用 `update_global_memory` 工具可让 AI 在对话中更新记忆内容。

```bash
echo "# 我的记忆\n\n- 我喜欢用 Python\n- 我的项目在 ~/projects" > ~/memory.md
```

### 自动启动脚本

创建 `~/start_server.sh`：

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

### SSH 远程管理

```bash
# 安装并启动 SSH
pkg install openssh -y
passwd  # 设置密码
sshd    # 启动服务

# 从电脑连接（默认端口 8022）
ssh -p 8022 u0_aXXX@手机IP
```

## 故障排查

### Chatbox 连接失败

```bash
# 确认服务运行
ps aux | grep server.py

# 确认 IP 地址
hostname -I

# 测试连接
curl http://localhost:5846/v1/models
```

### 工具不执行

查看日志中的 `[TOOL]` 信息：

```bash
tail -50 ~/server.log | grep TOOL
```

### AI 重复调用工具或出现 502 错误

通常是对话历史过长导致上游 token 超限。解决方法：在 Chatbox 对话设置中将"上下文消息数"设为 10 条以内。

### 查看实时日志

```bash
# 所有日志
tail -f ~/server.log

# 只看工具调用
tail -f ~/server.log | grep TOOL

# 只看错误
tail -f ~/server.log | grep -E "ERROR|WARNING"
```

## API 端点

### POST /v1/chat/completions

OpenAI 兼容的聊天接口。服务器完全无状态——对话历史由 Chatbox 管理。

### GET /v1/models

获取 `models_config.json` 中所有可用模型列表。

## 配置选项

在 `server.py` 顶部：

```python
DOWNLOAD_DIR = os.path.expanduser("~")  # 工作目录
TOOL_OUTPUT_MAX_CHARS = 8000            # 工具输出限制
```

## 项目结构

```
AIPhoneTools/
├── server.py                     # 主服务器
├── update_models.py              # 模型管理工具
├── models_config.json            # API 配置（不提交）
├── models_config.example.json    # 配置模板
├── requirements.txt              # Python 依赖
├── .gitignore                    # Git 忽略规则
├── README.md                     # 英文文档
└── README.zh-CN.md               # 本文档
```

## 注意事项

- **网络**：手机和电脑必须在同一局域网
- **电量**：长时间运行建议连接充电器
- **后台**：在系统设置中允许 Termux 后台运行
- **安全**：不要将 `models_config.json` 提交到公开仓库

## 更新日志

### v2.1.0 (2026-05-28)

- 记忆注入改为仅在新会话第一条消息时执行，避免长对话中重复消耗 token
- memory.md 读取上限从 2000 提升至 8000 字符
- `update_global_memory` 工具添加长度约束（总长不超过 8000 字符）

### v2.0.0 (2026-05-28)

- 无状态设计：对话历史由 Chatbox 管理，服务器不保存 session
- 多轮工具调用循环（最多 20 轮）
- 时间预算机制（50 秒自动中断）
- 工具执行期间 SSE 心跳（防止空闲超时）
- 自动加载 memory.md 到 system prompt
- 保留 Chatbox system 消息
- Termux-API 命令集成
- 工具调用进度实时显示

### v1.0.0 (2026-05-26)

- 6 种本地工具
- OpenAI 兼容接口

## 许可证

MIT License

## 相关链接

- [Termux 官网](https://termux.dev/)
- [Chatbox 官网](https://chatboxai.app/)
- [项目 GitHub](https://github.com/RayDutchman/AIPhoneTools)
- [English README](README.md)

# XMUOJ Helper

XMUOJ Helper 是一个面向 XMUOJ 竞赛/练习场景的半自动命令行工具。它可以登录账号、进入带密码的比赛、同步题目列表、导出题面与解题提示词、提交本地 C++ 代码、轮询判题结果，并在失败后生成修复提示词。

工具默认只把运行状态写入本地 `.xmuoj/` 目录，不会保存账号密码、比赛密码或 API Key。

## 功能特性

- 同步比赛题目列表并维护本地进度。
- 将题面转换为 Markdown，保存样例、限制、标签等信息。
- 为每道题生成适合交给代码模型的 C++17 解题提示词。
- 提交本地代码到 XMUOJ，并自动轮询 Accepted、Wrong Answer、Compile Error 等判题结果。
- 未通过时自动生成 repair prompt，方便继续修复。
- 提供交互模式，按题目顺序完成「生成提示词 -> 写代码 -> 提交 -> 修复」流程。
- 可选 DeepSeek/OpenAI-compatible 接口自动生成代码，默认 dry-run，不会直接提交。

## 环境要求

- Python 3.10 或更高版本。
- 可访问 XMUOJ 的网络环境。
- XMUOJ 账号、账号密码和比赛密码。
- 可选：DeepSeek API Key，仅在使用 `auto` 自动生成代码时需要。

脚本只使用 Python 标准库，不需要额外安装依赖。

## 快速开始

克隆仓库后进入项目目录：

```powershell
git clone <your-repo-url>
cd <your-repo-name>
```

设置 XMUOJ 登录信息：

```powershell
$env:XMUOJ_USERNAME="your_username"
$env:XMUOJ_PASSWORD="your_account_password"
$env:XMUOJ_CONTEST_PASSWORD="your_contest_password"
```

同步比赛题目：

```powershell
python tools/xmuoj_helper.py sync
```

进入推荐的交互流程：

```powershell
python tools/xmuoj_helper.py interactive
```

交互模式会显示当前题目的 prompt 文件和代码文件路径。你可以编辑生成的 `.xmuoj/solutions/<problem_id>.cpp`，然后在交互命令行输入 `submit` 提交。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python tools/xmuoj_helper.py sync` | 登录、进入比赛并缓存题目列表。 |
| `python tools/xmuoj_helper.py list` | 查看题目列表和本地完成状态。 |
| `python tools/xmuoj_helper.py prompt JD001` | 拉取指定题目，生成题面和解题提示词。 |
| `python tools/xmuoj_helper.py prompt` | 拉取下一道未完成题目。 |
| `python tools/xmuoj_helper.py submit JD001 --code .xmuoj/solutions/JD001.cpp` | 提交指定代码并等待判题结果。 |
| `python tools/xmuoj_helper.py poll <submission_id>` | 查询已有提交记录。 |
| `python tools/xmuoj_helper.py interactive` | 进入半自动交互刷题流程。 |
| `python tools/xmuoj_helper.py auto JD001` | 调用模型生成代码，但默认不提交。 |
| `python tools/xmuoj_helper.py auto JD001 --submit --max-retries 2` | 生成、提交并在失败时最多修复 2 次。 |

如果省略题号，`prompt`、`submit` 和 `auto` 会默认选择下一道未 Accepted、未 skipped 的题目。

## 配置项

通用配置可以通过命令行参数或环境变量传入：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `XMUOJ_BASE_URL` | `http://xmuoj.com` | XMUOJ 地址。 |
| `XMUOJ_CONTEST_ID` | `359` | 比赛 ID。 |
| `XMUOJ_USERNAME` | 空 | XMUOJ 用户名。 |
| `XMUOJ_PASSWORD` | 空 | XMUOJ 账号密码。 |
| `XMUOJ_CONTEST_PASSWORD` | 空 | 比赛密码。 |
| `XMUOJ_LANGUAGE` | `C++` | 提交语言。 |
| `XMUOJ_TIMEOUT` | `25` | 普通请求超时时间，单位秒。 |
| `XMUOJ_POLL_INTERVAL` | `2` | 轮询判题间隔，单位秒。 |
| `XMUOJ_POLL_TIMEOUT` | `120` | 单次提交最长等待时间，单位秒。 |

自动生成代码相关配置：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek API Key。 |
| `XMUOJ_SOLVER_API_KEY_ENV` | `DEEPSEEK_API_KEY` | 指定从哪个环境变量读取 API Key。 |
| `DEEPSEEK_API_BASE` | `https://api.deepseek.com` | OpenAI-compatible API 地址。 |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | 使用的模型名称。 |
| `XMUOJ_SOLVER_TEMPERATURE` | `0.2` | 生成温度。 |
| `XMUOJ_SOLVER_MAX_TOKENS` | `8192` | 最大输出 token 数。 |
| `XMUOJ_SOLVER_TIMEOUT` | `180` | 模型请求超时时间。 |
| `DEEPSEEK_THINKING` | `enabled` | 思考模式：`default`、`enabled` 或 `disabled`。 |
| `DEEPSEEK_REASONING_EFFORT` | `medium` | 推理强度：`low`、`medium` 或 `high`。 |

也可以把本地配置写到 `.xmuoj/env.ps1`，使用时手动加载：

```powershell
. .xmuoj/env.ps1
```

`.xmuoj/` 已被 `.gitignore` 忽略，适合存放本地配置、题面缓存和提交记录。

## 推荐流程

手动/半自动流程：

1. 运行 `python tools/xmuoj_helper.py interactive`。
2. 打开工具生成的 prompt 文件，把题面交给代码模型或自己编写解法。
3. 将代码写入对应的 `.xmuoj/solutions/<problem_id>.cpp`。
4. 在交互窗口输入 `submit`。
5. 如果未通过，使用工具生成的 repair prompt 继续修复。

模型自动生成流程：

```powershell
$env:DEEPSEEK_API_KEY="your_deepseek_api_key"

python tools/xmuoj_helper.py auto JD001
python tools/xmuoj_helper.py auto JD001 --submit --max-retries 2
python tools/xmuoj_helper.py auto --max-problems 3 --submit --delay 5
```

`auto` 默认是 dry-run：它会调用模型并写入代码文件，但不会提交到 OJ。只有显式添加 `--submit` 时才会提交。

## 生成文件

运行后会在 `.xmuoj/` 下生成以下内容：

| 路径 | 内容 |
| --- | --- |
| `.xmuoj/problem_list.json` | 缓存的比赛题目列表。 |
| `.xmuoj/progress.json` | 本地刷题进度和最近一次提交信息。 |
| `.xmuoj/problems/` | 转换后的 Markdown 题面。 |
| `.xmuoj/prompts/` | 解题 prompt 和修复 prompt。 |
| `.xmuoj/solutions/` | 可编辑的本地 C++ 代码文件。 |
| `.xmuoj/submissions/` | 已提交代码的快照。 |
| `.xmuoj/model_outputs/` | 模型原始输出和抽取后的代码。 |

## 项目结构

```text
.
├── tools/
│   ├── xmuoj_helper.py
│   └── xmuoj_helper_README.md
├── .gitignore
└── README.md
```

## 注意事项

- 请遵守课程、比赛和平台规则，仅在允许的练习或辅助场景中使用。
- 不要把 `.xmuoj/env.ps1`、账号密码、比赛密码、API Key 或个人提交记录上传到公开仓库。
- 如果公开发布仓库，建议补充 `LICENSE` 文件，明确他人是否可以使用、修改和分发。

## 故障排查

- `Contest access is still false`：检查比赛 ID、比赛密码和账号权限。
- `Code file does not exist`：先运行 `prompt <problem_id>` 生成默认代码文件，或用 `--code` 指定正确路径。
- `Solver returned empty content`：检查 API Key、模型名、API Base 和模型余额。
- 长时间停在判题中：可调大 `--poll-timeout`，例如 `--poll-timeout 300`。

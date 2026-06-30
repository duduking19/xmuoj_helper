# XMUOJ Helper

这个文件用于给直接打开 `tools/` 目录的用户做快速说明。

完整的项目介绍、配置项和使用流程请查看仓库根目录的
[`README.md`](../README.md)。

快速开始：

```powershell
$env:XMUOJ_USERNAME="your_username"
$env:XMUOJ_PASSWORD="your_account_password"
$env:XMUOJ_CONTEST_PASSWORD="your_contest_password"

python tools/xmuoj_helper.py sync
python tools/xmuoj_helper.py interactive
```

`auto` 模式默认是 dry-run：它会调用模型并写入代码文件，但不会提交到 OJ。
只有显式添加 `--submit` 时，工具才会提交生成的代码。

# XMUOJ Helper

This file is kept for users who open the `tools/` directory directly.

For the complete GitHub-ready documentation, see the repository root
[`README.md`](../README.md).

Quick start:

```powershell
$env:XMUOJ_USERNAME="your_username"
$env:XMUOJ_PASSWORD="your_account_password"
$env:XMUOJ_CONTEST_PASSWORD="your_contest_password"

python tools/xmuoj_helper.py sync
python tools/xmuoj_helper.py interactive
```

`auto` mode is dry-run by default. It only submits generated code when `--submit`
is explicitly provided.

# 题目记录网站

一个本地运行的 Python Flask 题库工具，支持：

- 自增序号记录题目
- 题干、ABCD 选项、解析、答案、标签
- 题干和解析上传图片
- 录入时 Markdown 与 LaTeX 实时预览
- 查看今天记录的所有题目
- 今日题目支持删除
- 历史页按天归档
- 一键把今天的题目导出为可交互选择题 Anki `.apkg` 包，牌组名为 `xx月xx日`

## 运行

```bash
cd /Users/terry/Documents/Codex/2026-07-09/bang/outputs/question_bank_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

打开：

```text
http://127.0.0.1:5000
```

## 数据位置

- 数据库：`data/questions.sqlite3`
- 上传图片：`uploads/`
- 导出的 Anki 包：`exports/`

## 说明

导出 Anki 时，会把今天的每一道题制作成一张选择题卡片：正面可点击 A/B/C/D 并即时显示对错，背面是答案、解析和标签。图片会随 `.apkg` 一起打包。

import os
import json
import re
from datetime import date
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError
import anthropic
from notion_client import Client as NotionClient

app = Flask(__name__)

LINE_TOKEN   = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_SECRET  = os.environ["LINE_CHANNEL_SECRET"]
CLAUDE_KEY   = os.environ["ANTHROPIC_API_KEY"]
NOTION_KEY   = os.environ.get("NOTION_API_KEY", "")

TASK_DB_ID   = os.environ.get("NOTION_TASK_DB_ID",   "caca43ba-9d1c-434d-b209-c1d408181c75")
FOOD_DB_ID   = os.environ.get("NOTION_FOOD_DB_ID",   "9dd3d44b-f900-469f-8866-39cb82944258")
MENTAL_DB_ID = os.environ.get("NOTION_MENTAL_DB_ID", "65bb5032-7f71-4088-a2e7-ecd27677f0b9")

line_config     = Configuration(access_token=LINE_TOKEN)
webhook_handler = WebhookHandler(LINE_SECRET)
claude          = anthropic.Anthropic(api_key=CLAUDE_KEY)
notion          = NotionClient(auth=NOTION_KEY) if NOTION_KEY else None

histories = {}

SYSTEM = """あなたは増山純さんの個人秘書「ジュン秘書」です。LINEで毎日サポートします。

対応範囲：
1 仕事・プライベートのタスク管理
2 食事の記録
3 メンタルケア
4 雑談・相談なんでも

返答フォーマット：
1行目: アクションJSON
2行目以降: ユーザーへのメッセージ

アクション例：
{"action":"add_task","title":"タスク名","due":"明日","category":"仕事"}
{"action":"add_food","meal":"昼食","items":["ラーメン","餃子"]}
{"action":"add_mental","mood":7,"note":"少し疲れ気味"}
{"action":"list_tasks"}
{"action":"complete_task","title":"タスク名"}
{"action":"none"}

ルール：
- mood は 0から10の整数
- category は仕事かプライベート
- 返答は短く温かく絵文字OK
"""


def parse_response(text):
    lines = text.strip().split("\n")
    action = {"action": "none"}
    msg_lines = []
    parsed = False
    for line in lines:
        if not parsed:
            m = re.search(r"\{[^{}]+\}", line)
            if m:
                try:
                    action = json.loads(m.group())
                    parsed = True
                    continue
                except json.JSONDecodeError:
                    pass
        msg_lines.append(line)
    return action, "\n".join(msg_lines).strip()


def add_task(title, due=None, category="仕事"):
    if not notion:
        return
    try:
        props = {
            "名前": {"title": [{"text": {"content": title}}]},
            "カテゴリ": {"select": {"name": category}},
            "ステータス": {"select": {"name": "未完了"}},
            "日付": {"date": {"start": date.today().isoformat()}},
        }
        if due:
            props["期限メモ"] = {"rich_text": [{"text": {"content": due}}]}
        notion.pages.create(parent={"database_id": TASK_DB_ID}, properties=props)
    except Exception as e:
        print(f"add_task error: {e}")


def add_food(meal, items):
    if not notion:
        return
    try:
        items_text = "、".join(items) if items else "記録なし"
        notion.pages.create(
            parent={"database_id": FOOD_DB_ID},
            properties={
                "食事": {"title": [{"text": {"content": items_text}}]},
                "区分": {"select": {"name": meal}},
                "日付": {"date": {"start": date.today().isoformat()}},
            },
        )
    except Exception as e:
        print(f"add_food error: {e}")


def add_mental(mood, note=""):
    if not notion:
        return
    try:
        notion.pages.create(
            parent={"database_id": MENTAL_DB_ID},
            properties={
                "記録": {"title": [{"text": {"content": f"気分:{mood}/10 {date.today()}"}}]},
                "気分スコア": {"number": mood},
                "メモ": {"rich_text": [{"text": {"content": note}}]},
                "日付": {"date": {"start": date.today().isoformat()}},
            },
        )
    except Exception as e:
        print(f"add_mental error: {e}")


def list_tasks():
    if not notion:
        return []
    try:
        res = notion.databases.query(
            database_id=TASK_DB_ID,
            filter={"property": "ステータス", "select": {"equals": "未完了"}},
            page_size=10,
        )
        result = []
        for page in res["results"]:
            t = page["properties"]["名前"]["title"]
            due = page["properties"]["期限メモ"]["rich_text"]
            name = t[0]["text"]["content"] if t else "？"
            due_str = f"({due[0]['text']['content']})" if due else ""
            result.append(f"{name}{due_str}")
        return result
    except Exception as e:
        print(f"list_tasks error: {e}")
        return []


def complete_task(title):
    if not notion:
        return False
    try:
        res = notion.databases.query(
            database_id=TASK_DB_ID,
            filter={"and": [
                {"property": "名前", "rich_text": {"contains": title}},
                {"property": "ステータス", "select": {"equals": "未完了"}},
            ]},
            page_size=1,
        )
        if res["results"]:
            notion.pages.update(
                page_id=res["results"][0]["id"],
                properties={"ステータス": {"select": {"name": "完了"}}},
            )
            return True
    except Exception as e:
        print(f"complete_task error: {e}")
    return False


def execute_action(action):
    act = action.get("action", "none")
    if act == "add_task":
        add_task(action.get("title", "タスク"), action.get("due"), action.get("category", "仕事"))
    elif act == "add_food":
        add_food(action.get("meal", "食事"), action.get("items", []))
    elif act == "add_mental":
        add_mental(int(action.get("mood", 5)), action.get("note", ""))
    elif act == "list_tasks":
        return list_tasks()
    elif act == "complete_task":
        complete_task(action.get("title", ""))
    return None


def chat(user_id, text):
    if user_id not in histories:
        histories[user_id] = []
    histories[user_id].append({"role": "user", "content": text})
    if len(histories[user_id]) > 20:
        histories[user_id] = histories[user_id][-20:]
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM,
        messages=histories[user_id],
    )
    reply_raw = response.content[0].text
    histories[user_id].append({"role": "assistant", "content": reply_raw})
    action, message = parse_response(reply_raw)
    extra = execute_action(action)
    if action.get("action") == "list_tasks":
        if isinstance(extra, list) and extra:
            task_lines = "\n".join(f"□ {t}" for t in extra)
            message = f"未完了タスク {len(extra)}件\n\n{task_lines}"
        else:
            message = "未完了タスクは0件です✨"
    return message or "了解です！何かあれば話しかけてください😊"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@webhook_handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    user_id = event.source.user_id
    reply = chat(user_id, event.message.text)
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)],
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

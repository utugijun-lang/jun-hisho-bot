import os
import json
import re
from datetime import date, datetime, timedelta, timezone
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

# ── 環境変数 ──────────────────────────────────
LINE_TOKEN   = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_SECRET  = os.environ["LINE_CHANNEL_SECRET"]
CLAUDE_KEY   = os.environ["ANTHROPIC_API_KEY"]
NOTION_KEY   = os.environ.get("NOTION_API_KEY", "")

# Notion DB ID（作成済み）
TASK_DB_ID   = os.environ.get("NOTION_TASK_DB_ID",   "caca43ba-9d1c-434d-b209-c1d408181c75")
FOOD_DB_ID   = os.environ.get("NOTION_FOOD_DB_ID",   "9dd3d44b-f900-469f-8866-39cb82944258")
MENTAL_DB_ID = os.environ.get("NOTION_MENTAL_DB_ID", "65bb5032-7f71-4088-a2e7-ecd27677f0b9")

# ── クライアント初期化 ────────────────────────
line_config     = Configuration(access_token=LINE_TOKEN)
webhook_handler = WebhookHandler(LINE_SECRET)
claude          = anthropic.Anthropic(api_key=CLAUDE_KEY)
notion          = NotionClient(auth=NOTION_KEY) if NOTION_KEY else None

# Google カレンダー
GCAL_PENDING_DB_ID = "12322bf9-11f6-471f-8086-b896b691680a"
JST                = timezone(timedelta(hours=9))
# ── 会話履歴（ユーザーごと、最大20ターン） ────
histories: dict[str, list] = {}

# ── Claude システムプロンプト ─────────────────
SYSTEM = """あなたは増山純さんの個人秘書「ジュン秘書」です。LINEで毎日サポートします。

対応範囲：
① 仕事・プライベートのタスク管理（追加・一覧・完了）
② 食事の記録（食べたものをメモ）
③ メンタルケア（気分チェック・励まし・相談）
④ Googleカレンダー管理（予定の追加・確認）
⑤ 雑談・相談なんでも

【必須ルール】返答は必ず下記フォーマットで返すこと：
1行目: アクションJSON（1行で）
2行目以降: ユーザーへのメッセージ（2〜3文・親しみやすく・絵文字OK）

アクションJSON一覧：
{"action":"add_task","title":"◯◯","due":"明日","category":"仕事"}    ← タスク追加
{"action":"add_food","meal":"昼食","items":["ラーメン","餃子"]}       ← 食事記録
{"action":"add_mental","mood":7,"note":"少し疲れ気味"}               ← 気分記録
{"action":"list_tasks"}                                               ← タスク一覧表示
{"action":"complete_task","title":"◯◯"}                              ← タスク完了
{"action":"add_event","title":"◯◯","start":"2026-04-08T10:00:00+09:00","end":"2026-04-08T11:00:00+09:00","description":""}  ← カレンダー追加
{"action":"list_events","days":7}                                     ← 予定確認
{"action":"none"}                                                     ← アクション不要

守るべきルール：
- mood は 0〜10 の整数（0=最悪、5=普通、10=最高）
- category は「仕事」か「プライベート」のどちらか
- タスクの期限が不明でも追加する（due は省略可）
- 【最重要】具体的な日時（○時、○日○時など）が含まれているメッセージは、「タスク追加」「予定追加」などの言葉があっても必ず add_event でGoogleカレンダーに追加する
- ユーザーが「タスク追加」とだけ送ってタイトルと日時が不明なら {"action":"none"} でタイトルを質問する
- 直前の会話でタスクタイトルを聞いた場合、次のユーザー発言をタイトルとして即座に add_task する
- 食事の items は具体的な料理名をリストで
- カレンダーのstart/endは必ずISO 8601形式（+09:00付き）で出力する
- 日時が曖昧（「明日10時」等）なら今日の日付を基準に計算して正確なdatetimeを生成する
- endが省略なら startの1時間後にする
- 返答は短く・温かく（2〜3文）・絵文字を適度に使う
- 雑談・相談には {"action":"none"} を使いしっかり答える
"""


# ── ユーティリティ ────────────────────────────

def parse_response(text: str) -> tuple[dict, str]:
    """Claude の返答から JSON アクションとメッセージを分離する"""
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


# ── Notion 操作 ──────────────────────────────

def add_task(title: str, due: str | None = None, category: str = "仕事"):
    if not notion:
        return
    try:
        props = {
            "名前":     {"title": [{"text": {"content": title}}]},
            "カテゴリ": {"select": {"name": category}},
            "ステータス": {"select": {"name": "未完了"}},
            "日付":     {"date": {"start": date.today().isoformat()}},
        }
        if due:
            props["期限メモ"] = {"rich_text": [{"text": {"content": due}}]}
        notion.pages.create(parent={"database_id": TASK_DB_ID}, properties=props)
    except Exception as e:
        print(f"[Notion] add_task error: {e}")


def add_food(meal: str, items: list[str]):
    if not notion:
        return
    try:
        items_text = "、".join(items) if items else "（記録なし）"
        notion.pages.create(
            parent={"database_id": FOOD_DB_ID},
            properties={
                "食事": {"title": [{"text": {"content": items_text}}]},
                "区分": {"select": {"name": meal}},
                "日付": {"date": {"start": date.today().isoformat()}},
            },
        )
    except Exception as e:
        print(f"[Notion] add_food error: {e}")


def add_mental(mood: int, note: str = ""):
    if not notion:
        return
    try:
        notion.pages.create(
            parent={"database_id": MENTAL_DB_ID},
            properties={
                "記録":     {"title": [{"text": {"content": f"気分:{mood}/10 — {date.today()}"}}]},
                "気分スコア": {"number": mood},
                "メモ":     {"rich_text": [{"text": {"content": note}}]},
                "日付":     {"date": {"start": date.today().isoformat()}},
            },
        )
    except Exception as e:
        print(f"[Notion] add_mental error: {e}")


def list_tasks() -> list[str]:
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
            t   = page["properties"]["名前"]["title"]
            due = page["properties"]["期限メモ"]["rich_text"]
            name     = t[0]["text"]["content"]   if t   else "？"
            due_str  = f"（{due[0]['text']['content']}）" if due else ""
            result.append(f"{name}{due_str}")
        return result
    except Exception as e:
        print(f"[Notion] list_tasks error: {e}")
        return []


def complete_task(title: str) -> bool:
    if not notion:
        return False
    try:
        res = notion.databases.query(
            database_id=TASK_DB_ID,
            filter={
                "and": [
                    {"property": "名前",     "rich_text": {"contains": title}},
                    {"property": "ステータス", "select":   {"equals": "未完了"}},
                ]
            },
            page_size=1,
        )
        if res["results"]:
            notion.pages.update(
                page_id=res["results"][0]["id"],
                properties={"ステータス": {"select": {"name": "完了"}}},
            )
            return True
    except Exception as e:
        print(f"[Notion] complete_task error: {e}")
    return False


# ── Google カレンダー操作 ─────────────────────

def add_event(title: str, start: str, end: str | None = None, description: str = "") -> bool:
    if not notion:
        return False
    try:
        notion.pages.create(
            parent={"database_id": GCAL_PENDING_DB_ID},
            properties={
                "タイトル": {"title": [{"text": {"content": title}}]},
                "開始日時": {"rich_text": [{"text": {"content": start}}]},
                "終了日時": {"rich_text": [{"text": {"content": end or ""}}]},
                "説明":     {"rich_text": [{"text": {"content": description}}]},
                "ステータス": {"select": {"name": "待機中"}},
                "作成日":   {"date": {"start": date.today().isoformat()}},
            }
        )
        print(f"[GCal] event queued in Notion: {title}")
        return True
    except Exception as e:
        print(f"[GCal] add_event Notion error: {e}")
        return False


def list_cal_events(days: int = 7) -> list[str]:
    if not notion:
        return []
    try:
        res = notion.databases.query(
            database_id=GCAL_PENDING_DB_ID,
            filter={"property": "ステータス", "select": {"equals": "待機中"}},
            page_size=10,
        )
        result = []
        for page in res["results"]:
            t = page["properties"]["タイトル"]["title"]
            s = page["properties"]["開始日時"]["rich_text"]
            title = t[0]["text"]["content"] if t else "？"
            start = s[0]["text"]["content"] if s else ""
            result.append(f"{start[:16]} {title}")
        return result
    except Exception as e:
        print(f"[GCal] list_events Notion error: {e}")
        return []

def execute_action(action: dict):
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
    elif act == "add_event":
        add_event(
            action.get("title", "予定"),
            action.get("start", ""),
            action.get("end"),
            action.get("description", ""),
        )
    elif act == "list_events":
        return list_cal_events(int(action.get("days", 7)))
    return None


# ── LINE × Claude ─────────────────────────────

def chat(user_id: str, text: str) -> str:
    """ユーザーメッセージを受けてClaude経由で返答を生成"""
    if user_id not in histories:
        histories[user_id] = []

    histories[user_id].append({"role": "user", "content": text})
    # 20ターンを超えたら古い履歴を削除
    if len(histories[user_id]) > 20:
        histories[user_id] = histories[user_id][-20:]

    system_with_date = f"今日の日付: {date.today().isoformat()}（日本時間）\n\n" + SYSTEM
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_with_date,
        messages=histories[user_id],
    )

    reply_raw = response.content[0].text
    histories[user_id].append({"role": "assistant", "content": reply_raw})

    action, message = parse_response(reply_raw)
    extra = execute_action(action)

    # タスク一覧の場合はメッセージを上書き
    if action.get("action") == "list_tasks":
        if isinstance(extra, list) and extra:
            task_lines = "\n".join(f"□ {t}" for t in extra)
            message = f"未完了タスク {len(extra)}件📋\n\n{task_lines}"
        else:
            message = "未完了タスクは0件です✨ 全部スッキリ！"

    return message or "了解です！何かあれば気軽に話しかけてください😊"


# ── Webhook エンドポイント ─────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    print(f"[DEBUG] sig_len={len(signature)} body_len={len(body)} secret_len={len(LINE_SECRET)}", flush=True)
    try:
        webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        print(f"[DEBUG] InvalidSignatureError: sig={signature[:20]}...", flush=True)
        abort(400)
    return "OK"


@webhook_handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    user_id = event.source.user_id
    reply   = chat(user_id, event.message.text)
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

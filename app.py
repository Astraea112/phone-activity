#!/usr/bin/env python3
"""手机活动上报服务 v2：收 iOS 快捷指令的 POST，存 SQLite，提供分析和可视化。"""

import os
import sqlite3
import threading
import time
import urllib.request
import ssl
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

CST = timezone(timedelta(hours=8))
EXPECTED_TOKEN = os.environ.get("REPORT_TOKEN", "")
DB_PATH = os.environ.get("DATA_DIR", "/tmp") + "/activity.db"
MAX_RECORDS = 2000  # 保留更多历史记录


@contextmanager
def get_db():
    """数据库连接上下文管理器，自动关闭。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            opened_at TEXT NOT NULL
        )
    """)
    try:
        yield conn
    finally:
        conn.close()


def require_token():
    if not EXPECTED_TOKEN:
        return None
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    if token != EXPECTED_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return None


def add_cors(response):
    """给所有响应加 CORS 头。"""
    if isinstance(response, tuple):
        resp = response[0]
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return response
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ─── 上报 ───

@app.route("/report", methods=["POST"])
def report():
    err = require_token()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    app_name = data.get("app_name") or data.get("app") or "unknown"
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute(
            "INSERT INTO phone_activity (app_name, opened_at) VALUES (?, ?)",
            (app_name, now),
        )
        conn.execute(f"""
            DELETE FROM phone_activity
            WHERE id NOT IN (
                SELECT id FROM phone_activity ORDER BY opened_at DESC LIMIT {MAX_RECORDS}
            )
        """)
        conn.commit()
    return jsonify({"status": "ok"})


# ─── 查询 ───

@app.route("/activity", methods=["GET"])
def activity():
    err = require_token()
    if err:
        return err

    limit = request.args.get("limit", 100, type=int)
    limit = min(limit, MAX_RECORDS)
    app_filter = request.args.get("app", "").strip()

    with get_db() as conn:
        if app_filter:
            rows = conn.execute(
                "SELECT app_name, opened_at FROM phone_activity WHERE app_name = ? ORDER BY opened_at DESC LIMIT ?",
                (app_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT app_name, opened_at FROM phone_activity ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return jsonify([{"app": r["app_name"], "time": r["opened_at"]} for r in rows])


@app.route("/activity/summary", methods=["GET"])
def activity_summary():
    """聚合摘要：最后活跃时间 + 最近 App + 今日统计。"""
    err = require_token()
    if err:
        return err

    today = datetime.now(CST).strftime("%Y-%m-%d")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT app_name, opened_at FROM phone_activity ORDER BY opened_at DESC LIMIT ?",
            (MAX_RECORDS,),
        ).fetchall()

        today_rows = conn.execute(
            "SELECT app_name, opened_at FROM phone_activity WHERE opened_at LIKE ? ORDER BY opened_at DESC",
            (today + "%",),
        ).fetchall()

    if not rows:
        return jsonify({
            "last_active": None,
            "recent_apps": [],
            "total_records": 0,
            "today": {"count": 0, "apps": [], "first_active": None, "last_active": None},
        })

    last_active = rows[0]["opened_at"]
    recent_apps = list(dict.fromkeys(r["app_name"] for r in rows[:20]))

    # 今日统计
    today_apps = {}
    for r in today_rows:
        name = r["app_name"]
        today_apps[name] = today_apps.get(name, 0) + 1

    today_sorted = sorted(today_apps.items(), key=lambda x: -x[1])

    return jsonify({
        "last_active": last_active,
        "recent_apps": recent_apps,
        "total_records": len(rows),
        "today": {
            "count": len(today_rows),
            "apps": [{"app": k, "opens": v} for k, v in today_sorted],
            "first_active": today_rows[-1]["opened_at"] if today_rows else None,
            "last_active": today_rows[0]["opened_at"] if today_rows else None,
        },
    })


@app.route("/activity/today", methods=["GET"])
def activity_today():
    """今天的详细活动。"""
    err = require_token()
    if err:
        return err

    today = datetime.now(CST).strftime("%Y-%m-%d")
    return _activity_for_date(today)


@app.route("/activity/date/<date>", methods=["GET"])
def activity_date(date):
    """查询指定日期的活动，格式 YYYY-MM-DD。"""
    err = require_token()
    if err:
        return err
    return _activity_for_date(date)


def _activity_for_date(date_str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT app_name, opened_at FROM phone_activity WHERE opened_at LIKE ? ORDER BY opened_at ASC",
            (date_str + "%",),
        ).fetchall()

    if not rows:
        return jsonify({"date": date_str, "count": 0, "apps": {}, "timeline": [], "screen_time_est": "0min"})

    # 统计每个 App 的打开次数
    app_counts = {}
    for r in rows:
        name = r["app_name"]
        app_counts[name] = app_counts.get(name, 0) + 1

    # 按次数排序
    app_sorted = sorted(app_counts.items(), key=lambda x: -x[1])

    # 估算屏幕时间：两条记录之间如果间隔 < 10 分钟就算在用
    total_minutes = 0
    for i in range(len(rows) - 1):
        t1 = datetime.strptime(rows[i]["opened_at"], "%Y-%m-%d %H:%M:%S")
        t2 = datetime.strptime(rows[i + 1]["opened_at"], "%Y-%m-%d %H:%M:%S")
        gap = (t2 - t1).total_seconds() / 60
        if gap < 10:
            total_minutes += gap

    # 格式化屏幕时间
    hours = int(total_minutes // 60)
    mins = int(total_minutes % 60)
    screen_time = f"{hours}h{mins}min" if hours > 0 else f"{mins}min"

    # 时间线：按小时分组
    hourly = {}
    for r in rows:
        hour = r["opened_at"][11:13]
        hourly[hour] = hourly.get(hour, 0) + 1

    return jsonify({
        "date": date_str,
        "count": len(rows),
        "apps": [{"app": k, "opens": v} for k, v in app_sorted],
        "screen_time_est": screen_time,
        "hourly": hourly,
        "first_active": rows[0]["opened_at"],
        "last_active": rows[-1]["opened_at"],
        "timeline": [{"app": r["app_name"], "time": r["opened_at"]} for r in rows],
    })


@app.route("/activity/stats", methods=["GET"])
def activity_stats():
    """整体统计：App 使用排行、日均打开次数、活跃天数。"""
    err = require_token()
    if err:
        return err

    with get_db() as conn:
        rows = conn.execute(
            "SELECT app_name, opened_at FROM phone_activity ORDER BY opened_at ASC"
        ).fetchall()

    if not rows:
        return jsonify({"total": 0, "app_ranking": [], "active_days": 0, "daily_avg": 0})

    # App 使用排行
    app_counts = {}
    daily_counts = {}
    for r in rows:
        name = r["app_name"]
        day = r["opened_at"][:10]
        app_counts[name] = app_counts.get(name, 0) + 1
        daily_counts[day] = daily_counts.get(day, 0) + 1

    app_sorted = sorted(app_counts.items(), key=lambda x: -x[1])
    active_days = len(daily_counts)
    daily_avg = round(len(rows) / active_days, 1) if active_days else 0

    # 最近7天趋势
    today = datetime.now(CST).date()
    week_trend = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        week_trend.append({"date": d, "count": daily_counts.get(d, 0)})

    return jsonify({
        "total": len(rows),
        "app_ranking": [{"app": k, "opens": v} for k, v in app_sorted[:15]],
        "active_days": active_days,
        "daily_avg": daily_avg,
        "first_record": rows[0]["opened_at"],
        "last_record": rows[-1]["opened_at"],
        "week_trend": week_trend,
    })


# ─── 可视化面板 ───

@app.route("/", methods=["GET"])
def dashboard():
    """简洁的可视化面板。"""
    err = require_token()
    if err:
        return err

    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>📱 Activity</title>
<style>
:root {
  --bg: #0a0a0f;
  --card: #12121a;
  --border: #1e1e2e;
  --text: #e0e0e8;
  --dim: #6e6e8a;
  --accent: #a78bfa;
  --accent2: #818cf8;
  --green: #34d399;
  --red: #f87171;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, 'SF Pro', 'Helvetica Neue', sans-serif;
  background: var(--bg);
  color: var(--text);
  padding: 16px;
  max-width: 500px;
  margin: 0 auto;
  -webkit-font-smoothing: antialiased;
}
h1 { font-size: 20px; margin-bottom: 16px; font-weight: 600; }
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 12px;
}
.card h2 { font-size: 13px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; font-weight: 500; }
.big { font-size: 28px; font-weight: 700; color: var(--accent); }
.row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; }
.row:not(:last-child) { border-bottom: 1px solid var(--border); }
.app-name { font-size: 14px; }
.count { font-size: 14px; color: var(--accent2); font-weight: 600; }
.time { font-size: 12px; color: var(--dim); }
.bar-wrap { display: flex; align-items: center; gap: 8px; flex: 1; margin-left: 12px; }
.bar { height: 6px; border-radius: 3px; background: var(--accent); min-width: 2px; }
.timeline { display: flex; gap: 2px; align-items: end; height: 40px; margin-top: 8px; }
.timeline .col { flex: 1; background: var(--accent); border-radius: 2px 2px 0 0; min-height: 2px; opacity: 0.7; }
.timeline .col:hover { opacity: 1; }
.status { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.dot { width: 6px; height: 6px; border-radius: 50%; }
.dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
.dot.off { background: var(--red); }
.loading { text-align: center; color: var(--dim); padding: 40px 0; }
.empty { text-align: center; color: var(--dim); padding: 20px 0; font-size: 14px; }
</style>
</head>
<body>
<h1>📱 Activity</h1>
<div id="app"><div class="loading">加载中...</div></div>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const qs = token ? '?token=' + encodeURIComponent(token) : '';

async function load() {
  try {
    const [summaryRes, todayRes, statsRes] = await Promise.all([
      fetch('/activity/summary' + qs),
      fetch('/activity/today' + qs),
      fetch('/activity/stats' + qs),
    ]);
    const summary = await summaryRes.json();
    const today = await todayRes.json();
    const stats = await statsRes.json();
    render(summary, today, stats);
  } catch(e) {
    document.getElementById('app').innerHTML = '<div class="empty">无法加载数据</div>';
  }
}

function render(summary, today, stats) {
  const el = document.getElementById('app');
  if (!summary.last_active) {
    el.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }

  // 判断是否在线：最后活跃5分钟内
  const last = new Date(summary.last_active.replace(' ', 'T') + '+08:00');
  const now = new Date();
  const diffMin = (now - last) / 60000;
  const isOnline = diffMin < 5;
  const statusText = isOnline ? '在线' : (diffMin < 60 ? Math.round(diffMin) + '分钟前' : (diffMin < 1440 ? Math.round(diffMin/60) + '小时前' : Math.round(diffMin/1440) + '天前'));

  // App 排行的最大值
  const maxOpens = today.apps && today.apps.length > 0 ? today.apps[0].opens : 1;

  // 小时时间线
  let hourlyHtml = '';
  if (today.hourly) {
    const maxH = Math.max(...Object.values(today.hourly), 1);
    for (let h = 0; h < 24; h++) {
      const key = String(h).padStart(2, '0');
      const val = today.hourly[key] || 0;
      const pct = (val / maxH) * 100;
      hourlyHtml += '<div class="col" style="height:' + (val ? Math.max(pct, 8) : 0) + '%" title="' + key + ':00 - ' + val + '次"></div>';
    }
  }

  let html = '';

  // 状态卡片
  html += '<div class="card"><div class="row"><div class="status"><div class="dot ' + (isOnline ? 'on' : 'off') + '"></div>' + statusText + '</div>';
  if (summary.last_active) {
    html += '<div class="time">' + summary.last_active + '</div>';
  }
  html += '</div></div>';

  // 今日概览
  html += '<div class="card"><h2>今日</h2>';
  html += '<div class="row"><div><div class="big">' + (today.count || 0) + '</div><div class="time">次打开</div></div>';
  html += '<div style="text-align:right"><div class="big" style="font-size:20px">' + (today.screen_time_est || '0min') + '</div><div class="time">估算屏幕时间</div></div></div>';
  if (hourlyHtml) {
    html += '<div class="timeline">' + hourlyHtml + '</div>';
    html += '<div style="display:flex;justify-content:space-between;margin-top:4px"><div class="time">0</div><div class="time">6</div><div class="time">12</div><div class="time">18</div><div class="time">24</div></div>';
  }
  html += '</div>';

  // 今日 App 排行
  if (today.apps && today.apps.length > 0) {
    html += '<div class="card"><h2>App 使用</h2>';
    today.apps.forEach(function(a) {
      const pct = (a.opens / maxOpens) * 100;
      html += '<div class="row"><span class="app-name">' + a.app + '</span>';
      html += '<div class="bar-wrap"><div class="bar" style="width:' + pct + '%"></div></div>';
      html += '<span class="count">' + a.opens + '</span></div>';
    });
    html += '</div>';
  }

  // 7日趋势
  if (stats.week_trend) {
    const maxW = Math.max(...stats.week_trend.map(d => d.count), 1);
    html += '<div class="card"><h2>7日趋势</h2><div class="timeline" style="height:50px">';
    stats.week_trend.forEach(function(d) {
      const pct = (d.count / maxW) * 100;
      const label = d.date.slice(5);
      html += '<div class="col" style="height:' + (d.count ? Math.max(pct, 8) : 0) + '%" title="' + label + ' - ' + d.count + '次"></div>';
    });
    html += '</div><div style="display:flex;justify-content:space-between;margin-top:4px">';
    stats.week_trend.forEach(function(d) {
      html += '<div class="time">' + d.date.slice(5) + '</div>';
    });
    html += '</div></div>';
  }

  // 总计
  html += '<div class="card"><h2>总计</h2>';
  html += '<div class="row"><span class="app-name">记录总数</span><span class="count">' + stats.total + '</span></div>';
  html += '<div class="row"><span class="app-name">活跃天数</span><span class="count">' + stats.active_days + '</span></div>';
  html += '<div class="row"><span class="app-name">日均打开</span><span class="count">' + stats.daily_avg + '</span></div>';
  html += '</div>';

  el.innerHTML = html;
}

load();
setInterval(load, 60000);  // 每分钟刷新
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "version": "2.1"})



# ─── 保活：防止 Render 免费版冻结服务 ───

RENDER_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    "https://phone-activity-ptd4.onrender.com"
)

def keep_alive():
    """每 13 分钟自我 ping，防止 Render 冻结进程。"""
    ctx = ssl.create_default_context()
    while True:
        time.sleep(780)  # 13 分钟
        try:
            urllib.request.urlopen(f"{RENDER_URL}/ping", timeout=10, context=ctx)
        except Exception:
            pass

_keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
_keep_alive_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

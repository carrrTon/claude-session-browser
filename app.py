#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude/projects"))).expanduser()
SETTINGS_PATH = APP_DIR / "settings.local.json"
CLI_COMMAND = os.environ.get("CLAUDE_BROWSER_CLI", "claude")
AUTH_TOKEN = secrets.token_urlsafe(24)
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".log", ".csv", ".tsv"}
MAX_TEXT_BYTES = 1024 * 1024
MAX_LISTED_FILES = 200
MAX_REQUEST_BYTES = 64 * 1024
SERVER = None
PAGE_CLIENTS = set()
PAGE_LOCK = threading.Lock()


def empty_preferences():
    return {"pinnedProjects": []}


def normalize_preferences(value):
    if not isinstance(value, dict):
        return empty_preferences()
    pinned = value.get("pinnedProjects", [])
    if not isinstance(pinned, list):
        pinned = []
    return {"pinnedProjects": [item for item in pinned if isinstance(item, str)]}


def read_settings():
    if not SETTINGS_PATH.exists():
        return {"preferences": empty_preferences()}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"preferences": empty_preferences()}
    if not isinstance(data, dict):
        return {"preferences": empty_preferences()}
    return {"preferences": normalize_preferences(data.get("preferences"))}


def write_settings(settings):
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def current_preferences():
    return read_settings()["preferences"]


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def one_line(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def iso_from_mtime(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def is_loopback_host(host):
    return host in {"127.0.0.1", "localhost", "::1"}


def preview_unavailable_reason(path):
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return "文件超过 1MB"
    except OSError:
        return "无法读取文件信息"
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return "暂不支持该文件类型"
    return "无法读取文件内容"


def safe_file_node(path, root, sessions_by_file):
    try:
        return file_node(path, root, sessions_by_file)
    except OSError as exc:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = path.name
        return {
            "id": str(path),
            "type": "file",
            "name": path.name,
            "path": str(path),
            "relativePath": rel,
            "updated": datetime.now(timezone.utc).isoformat(),
            "size": 0,
            "text": None,
            "textAvailable": False,
            "previewReason": f"无法读取：{exc}",
        }


def display_project_path(dirname):
    if dirname.startswith("-"):
        return "/" + dirname[1:].replace("--", "/").replace("-", "/")
    return dirname


def project_name(dirname):
    path = display_project_path(dirname)
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else dirname


def text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                kind = item.get("type")
                if kind == "text":
                    parts.append(item.get("text", ""))
                elif kind == "tool_use":
                    parts.append(f"[工具调用] {item.get('name', '')}")
                elif kind == "tool_result":
                    parts.append("[工具结果] " + text_from_content(item.get("content", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(f"[{kind or '内容'}]")
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return text_from_content(content.get("content") or content.get("text") or "")
    return "" if content is None else str(content)


def read_session(path):
    title = None
    custom_title = None
    cwd = None
    first_user = None
    messages = []
    started = None
    updated = None
    tool_count = 0

    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = parse_time(obj.get("timestamp"))
                if timestamp:
                    started = timestamp if started is None or timestamp < started else started
                    updated = timestamp if updated is None or timestamp > updated else updated

                cwd = cwd or obj.get("cwd")

                if obj.get("type") == "custom-title":
                    custom_title = obj.get("customTitle") or custom_title
                    continue

                if obj.get("type") == "ai-title":
                    title = obj.get("aiTitle") or title
                    continue

                if obj.get("type") not in ("user", "assistant"):
                    continue

                message = obj.get("message") or {}
                role = message.get("role") or obj.get("type")
                text = text_from_content(message.get("content"))
                if not text.strip():
                    continue

                tool_count += text.count("[工具调用]") + text.count("[工具结果]")
                if role == "user" and first_user is None:
                    first_user = text.strip()

                messages.append({
                    "role": role,
                    "time": obj.get("timestamp"),
                    "text": text,
                })
    except OSError:
        pass

    stat = path.stat()
    updated = updated or datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    started = started or updated
    title = custom_title or title or first_user or path.stem

    return {
        "id": str(path),
        "file": str(path),
        "sessionId": path.stem,
        "title": one_line(title, 80),
        "firstUser": one_line(first_user or "", 160),
        "cwd": cwd or "",
        "started": started.isoformat(),
        "updated": updated.isoformat(),
        "messageCount": len(messages),
        "toolCount": tool_count,
        "size": stat.st_size,
        "messages": messages,
    }


def read_text_file(path):
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        if path.suffix.lower() not in TEXT_SUFFIXES:
            return None
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    except OSError:
        return None


def list_child_nodes(path, root, sessions_by_file):
    try:
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    return [safe_file_node(child, root, sessions_by_file) for child in children]


def file_node(path, root, sessions_by_file):
    stat = path.stat()
    rel = str(path.relative_to(root))
    node_id = str(path)
    if path.is_dir():
        children = list_child_nodes(path, root, sessions_by_file)
        return {
            "id": node_id,
            "type": "directory",
            "name": path.name,
            "path": str(path),
            "relativePath": rel,
            "updated": iso_from_mtime(path),
            "children": children,
        }

    session = sessions_by_file.get(str(path))
    if session:
        return {
            "id": node_id,
            "type": "session",
            "name": session["title"],
            "fileName": path.name,
            "path": str(path),
            "relativePath": rel,
            "updated": session["updated"],
            "size": stat.st_size,
            "session": session,
        }

    text = read_text_file(path)
    return {
        "id": node_id,
        "type": "file",
        "name": path.name,
        "path": str(path),
        "relativePath": rel,
        "updated": iso_from_mtime(path),
        "size": stat.st_size,
        "text": text,
        "textAvailable": text is not None,
        "previewReason": "" if text is not None else preview_unavailable_reason(path),
    }


def scan():
    projects = []
    preferences = current_preferences()
    if not ROOT.exists():
        return {"root": str(ROOT), "preferences": preferences, "projects": []}

    for project_dir in sorted((path for path in ROOT.iterdir() if path.is_dir() and project_name(path.name) != ".claude"), key=lambda p: display_project_path(p.name).lower()):
        sessions = []
        for path in sorted(project_dir.rglob("*.jsonl")):
            try:
                sessions.append(read_session(path))
            except OSError:
                continue
        sessions_by_file = {session["file"]: session for session in sessions}
        children = list_child_nodes(project_dir, project_dir, sessions_by_file)
        if not children:
            continue
        sessions.sort(key=lambda item: item["updated"], reverse=True)
        latest = max((node["updated"] for node in children), default=iso_from_mtime(project_dir))
        projects.append({
            "id": str(project_dir),
            "dir": str(project_dir),
            "name": project_name(project_dir.name),
            "pathHint": display_project_path(project_dir.name),
            "sessionCount": len(sessions),
            "hasMemory": (project_dir / "memory").exists(),
            "updated": latest,
            "sessions": sessions,
            "children": children,
        })

    return {"root": str(ROOT), "preferences": preferences, "projects": projects}


def project_ids():
    if not ROOT.exists():
        return set()
    ids = set()
    for path in ROOT.iterdir():
        if path.is_dir() and project_name(path.name) != ".claude":
            ids.add(str(path))
    return ids


def set_project_pinned(project_id, pinned):
    if not project_id:
        raise ValueError("缺少项目")
    allowed = project_ids()
    if project_id not in allowed:
        raise ValueError("只能置顶当前 Claude projects 目录内的项目")
    settings = read_settings()
    preferences = settings["preferences"]
    pinned_projects = [item for item in preferences.get("pinnedProjects", []) if item in allowed]
    if pinned:
        if project_id not in pinned_projects:
            pinned_projects.append(project_id)
    else:
        pinned_projects = [item for item in pinned_projects if item != project_id]
    settings["preferences"] = {"pinnedProjects": pinned_projects}
    write_settings(settings)


def open_path_in_finder(path_value):
    root = ROOT.resolve()
    if not path_value:
        raise ValueError("缺少路径")
    target = Path(path_value).expanduser().resolve()
    if target != root and root not in target.parents:
        raise ValueError("只能打开 Claude projects 目录内的文件夹")
    if not target.exists() or not target.is_dir():
        raise ValueError("只能在访达打开存在的文件夹")
    subprocess.run(["open", str(target)], check=True, capture_output=True, text=True)


def rename_session(path_value, title):
    root = ROOT.resolve()
    if not path_value:
        raise ValueError("缺少路径")
    target = Path(path_value).expanduser().resolve()
    if target == root or root not in target.parents:
        raise ValueError("只能重命名 Claude projects 目录内的会话")
    if target.suffix != ".jsonl" or not target.exists():
        raise ValueError("只能重命名存在的 jsonl 会话文件")
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("名称不能为空")
    if len(title) > 120:
        raise ValueError("名称不能超过 120 个字符")
    record = {"type": "custom-title", "sessionId": target.stem, "customTitle": title}
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def schedule_shutdown_if_idle(client_id):
    def check():
        time.sleep(2)
        with PAGE_LOCK:
            if client_id:
                PAGE_CLIENTS.discard(client_id)
            should_shutdown = not PAGE_CLIENTS
        if should_shutdown and SERVER:
            SERVER.shutdown()
    threading.Thread(target=check, daemon=True).start()


def open_session_in_terminal(path_value):
    root = ROOT.resolve()
    if not path_value:
        raise ValueError("缺少路径")
    target = Path(path_value).expanduser().resolve()
    if target == root or root not in target.parents:
        raise ValueError("只能打开 Claude projects 目录内的会话")
    if target.suffix != ".jsonl" or not target.exists():
        raise ValueError("只能打开存在的 jsonl 会话文件")
    session = read_session(target)
    cwd = session.get("cwd") or str(target.parent)
    session_id = session.get("sessionId") or target.stem
    resume_command = f"cd {shlex.quote(cwd)} && {shlex.quote(CLI_COMMAND)} --resume {shlex.quote(session_id)}"
    command = f"zsh -ic {shlex.quote(resume_command)}"
    script = f'''
        tell application "Terminal"
            activate
            do script {json.dumps(command, ensure_ascii=False)}
        end tell
    '''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)


def trash_path(path_value):
    root = ROOT.resolve()
    if not path_value:
        raise ValueError("缺少路径")
    target = Path(path_value).expanduser().resolve()
    if target == root or root not in target.parents:
        raise ValueError("只能删除 Claude projects 目录内的项目、文件或文件夹")
    if not target.exists():
        raise FileNotFoundError("路径不存在")
    subprocess.run([
        "osascript",
        "-e",
        f'tell application "Finder" to delete POSIX file {json.dumps(str(target))}',
    ], check=True, capture_output=True, text=True)


def contained_files(path_value):
    root = ROOT.resolve()
    if not path_value:
        raise ValueError("缺少路径")
    target = Path(path_value).expanduser().resolve()
    if target == root or root not in target.parents:
        raise ValueError("只能查看 Claude projects 目录内的项目、文件或文件夹")
    if not target.exists():
        raise FileNotFoundError("路径不存在")
    if target.is_file():
        return {"files": [str(target)], "total": 1, "limited": False}
    files = []
    total = 0
    for path in sorted(target.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file():
            continue
        total += 1
        if len(files) < MAX_LISTED_FILES:
            files.append(str(path))
    return {"files": files, "total": total, "limited": total > len(files)}


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Claude 会话管理器</title>
<style>
:root{--line:#e8e5df;--text:#252525;--muted:#8b8b8b;--bubble:#f1f1f1;--selected:#e8e1d8}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"PingFang SC",sans-serif;color:var(--text);background:#fff;overflow:hidden}.app{height:100vh;display:grid;grid-template-columns:380px minmax(0,1fr);overflow:hidden}.side{height:100vh;min-width:0;background:linear-gradient(180deg,#eef5f8,#f2ece6);border-right:1px solid var(--line);display:flex;flex-direction:column;position:sticky;left:0;top:0;overflow:hidden}.actions{padding:6px 18px 10px;display:grid;gap:7px;flex:0 0 auto}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px}.nav{color:#4e5860;font-weight:650}.search{width:100%;border:0;background:rgba(255,255,255,.75);border-radius:10px;padding:8px 12px;outline:0}.meta{display:flex;align-items:center;justify-content:flex-end;gap:6px}.delete-icon{border:0;background:transparent;width:13px;height:13px;padding:0;cursor:pointer;background-image:url('data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%201024%201024%22%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%3E%3Cpath%20d%3D%22M512%201024a512%20512%200%201%201%20512-512%20512%20512%200%200%201-512%20512z%22%20fill%3D%22%23FFE8E6%22/%3E%3Cpath%20d%3D%22M710.509714%20313.490286a28.379429%2028.379429%200%200%200-40.155428%200l-157.257143%20157.257143-157.257143-157.257143a28.306286%2028.306286%200%201%200-40.009143%2040.009143l157.330286%20157.257142-157.330286%20157.330286a28.379429%2028.379429%200%200%200%2040.155429%2040.155429l157.330285-157.330286%20155.501715%20155.501714a28.306286%2028.306286%200%200%200%2040.009143-40.009143L553.325714%20510.902857l157.257143-157.257143a28.379429%2028.379429%200%200%200-0.073143-40.155428z%22%20fill%3D%22%23FF7A65%22/%3E%3C/svg%3E');background-size:13px 13px;background-repeat:no-repeat;background-position:center}.delete-icon:hover{opacity:.75}.section{padding:8px 18px 4px;color:#9a9a9a;font-weight:650;flex:0 0 auto}.tree{flex:1;overflow:auto;padding-bottom:16px}.item{padding:4px 18px;display:grid;grid-template-columns:1fr auto;gap:4px 10px;border-left:3px solid transparent;cursor:pointer}.item:hover{background:rgba(255,255,255,.45)}.item.active{background:var(--selected);border-left-color:#b7a694}.title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{font-size:12px;color:var(--muted);white-space:nowrap}.sub{grid-column:1/3;font-size:12px;color:#777;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.project{padding-top:6px;padding-bottom:5px}.project .title{font-weight:650}.folder .title:before,.project .title:before{content:'▸';display:inline-block;width:20px;color:#777;font-size:1.5em;line-height:.8;vertical-align:-1px}.folder.expanded .title:before,.project.expanded .title:before{content:'▾'}.file .title:before{content:'·';display:inline-block;width:20px;color:#aaa}.memory-file .title:before{content:'';display:inline-block;width:0}.session .title:before{content:'';display:inline-block;width:0}.main{height:100vh;min-width:0;display:grid;grid-template-rows:58px minmax(0,1fr);background:#fff;overflow:hidden}.top{border-bottom:1px solid #f0f0f0;padding:0 24px;display:flex;align-items:center;justify-content:space-between;gap:20px;overflow:hidden}.heading{min-width:0}.heading h1{font-size:16px;margin:0;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.path{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:58vw}.stats{display:flex;gap:8px;align-items:center;white-space:nowrap}.pill,.btn{border:1px solid #dedede;background:#fafafa;border-radius:999px;padding:3px 9px;color:#666;font-size:12px}.btn{cursor:pointer}.sort-btn.active{border-color:#b7a694;background:#e8e1d8;color:#4f4338}.delete-btn{border-color:#ffd1cc;background:#ffe8e6;color:#ff7a65}.chat{min-height:0;overflow:auto;padding:34px 44px}.empty{text-align:center;color:var(--muted);margin-top:18vh}.msg{max-width:980px;margin:0 auto 24px}.msg.user{display:flex;justify-content:flex-end}.role{font-size:12px;color:var(--muted);margin-bottom:6px}.bubble{max-width:760px;background:var(--bubble);border-radius:18px;padding:14px 16px;white-space:pre-wrap;overflow-wrap:anywhere}.assistant .bubble{max-width:920px;background:#fff;border-radius:0;padding:0}.tool{display:inline-block;color:#666;background:#f5f5f5;border:1px solid #e7e7e7;border-radius:7px;padding:1px 6px}.file-view{max-width:980px;margin:0 auto}.file-content{background:#fbfbfb;border:1px solid #e8e8e8;border-radius:12px;padding:16px;white-space:pre-wrap;overflow:auto}pre{background:#f6f6f6;border:1px solid #e8e8e8;border-radius:12px;padding:14px;overflow:auto;white-space:pre-wrap}.small{font-size:12px;color:var(--muted);padding:8px 18px}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.28);display:none;align-items:center;justify-content:center;z-index:10}.modal{width:min(720px,calc(100vw - 48px));max-height:80vh;background:#fff;border-radius:14px;box-shadow:0 18px 60px rgba(0,0,0,.22);padding:20px;display:flex;flex-direction:column;gap:12px}.modal h2{font-size:16px;margin:0}.modal-path{font-size:12px;color:#666;background:#f7f7f7;border-radius:8px;padding:8px;word-break:break-all}.modal-files{max-height:260px;overflow:auto;border:1px solid #eee;border-radius:8px;padding:8px;font-size:12px;color:#666;background:#fbfbfb;white-space:pre-wrap}.modal-actions{display:flex;justify-content:flex-end;gap:10px}.danger{border:1px solid #d33;background:#d33;color:white;border-radius:8px;padding:7px 12px;cursor:pointer}.cancel{border:1px solid #ddd;background:#fafafa;color:#555;border-radius:8px;padding:7px 12px;cursor:pointer}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="actions">
      <div class="nav">搜索</div>
      <input id="query" class="search" placeholder="搜索左侧项目、文件夹、文件和会话" />
    </div>
    <div id="tree" class="tree"></div>
  </aside>
  <main class="main">
    <div class="top"><div class="heading"><h1 id="title">Claude 会话管理器</h1><div id="path" class="path">管理本地 Claude Code 项目数据</div></div><div id="stats" class="stats"></div></div>
    <div id="chat" class="chat"><div class="empty">选择左侧文件或会话查看内容</div></div>
  </main>
</div>
<div id="confirmModal" class="modal-backdrop">
  <div class="modal">
    <h2 id="modalTitle">确定删除这个项目吗？</h2>
    <div>名称：<span id="modalName"></span></div>
    <div class="modal-path">路径：<span id="modalPath"></span></div>
    <div id="modalFiles" class="modal-files" style="display:none"></div>
    <div class="modal-actions"><button id="cancelDelete" class="cancel" type="button">取消</button><button id="confirmDelete" class="danger" type="button">移到废纸篓</button></div>
  </div>
</div>
<script>
let data=null, selectedProject=null, selectedNode=null, query='', expanded=new Set(), pinnedProjectIds=new Set(), clientId=crypto.randomUUID(), pinnedSortKey='time', pinnedSortDirection='desc', regularSortKey='time', regularSortDirection='desc';
const AUTH_TOKEN=new URLSearchParams(location.search).get('token')||'__AUTH_TOKEN__';
const el=id=>document.getElementById(id);
function escapeHtml(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function relTime(iso){const seconds=(Date.now()-new Date(iso))/1000;if(seconds<3600)return Math.max(1,Math.floor(seconds/60))+' 分钟';if(seconds<86400)return Math.floor(seconds/3600)+' 小时';if(seconds<2592000)return Math.floor(seconds/86400)+' 天';return Math.floor(seconds/2592000)+' 月'}
function includes(text){return !query || (text||'').toLowerCase().includes(query.toLowerCase())}
function sizeText(n){if(n==null)return '';if(n<1024)return n+' B';if(n<1048576)return Math.round(n/1024)+' KB';return (n/1048576).toFixed(1)+' MB'}
function authBody(body={}){return JSON.stringify({...body,token:AUTH_TOKEN})}
function authUrl(path){return `${path}${path.includes('?')?'&':'?'}token=${encodeURIComponent(AUTH_TOKEN)}`}
function sortedProjectGroup(projects,key,direction){const items=[...projects];items.sort((a,b)=>{let result=0;if(key==='path'){result=(a.pathHint||a.name||'').localeCompare(b.pathHint||b.name||'');}else{result=new Date(a.updated)-new Date(b.updated);}return direction==='asc'?result:-result});return items}
function sortedNodes(nodes,key,direction){const items=[...nodes];items.sort((a,b)=>{const aFolder=a.type==='directory';const bFolder=b.type==='directory';if(aFolder!==bFolder)return aFolder?-1:1;let result=0;if(key==='path'){result=(a.relativePath||a.name||'').localeCompare(b.relativePath||b.name||'');}else{result=new Date(a.updated||0)-new Date(b.updated||0);}return direction==='asc'?result:-result});return items}
function syncPreferences(){pinnedProjectIds=new Set((data&&data.preferences&&Array.isArray(data.preferences.pinnedProjects))?data.preferences.pinnedProjects:[])}
function pinnedProjects(){return (data&&data.projects?data.projects:[]).filter(project=>isProjectPinned(project))}
function regularProjects(){return (data&&data.projects?data.projects:[]).filter(project=>!isProjectPinned(project))}
function isProjectPinned(project){return Boolean(project&&pinnedProjectIds.has(project.id))}
function groupSortButtons(group,key,direction){return `<button class="btn sort-btn ${key==='time'?'active':''}" type="button" onclick="setGroupSort('${group}','time')">时间 ${key==='time'&&direction==='asc'?'↑':'↓'}</button><button class="btn sort-btn ${key==='path'?'active':''}" type="button" onclick="setGroupSort('${group}','path')">路径 ${key==='path'&&direction==='asc'?'↑':'↓'}</button>`}
function setGroupSort(group,key){if(group==='pinned'){if(pinnedSortKey===key){pinnedSortDirection=pinnedSortDirection==='desc'?'asc':'desc'}else{pinnedSortKey=key;pinnedSortDirection='desc'}}else{if(regularSortKey===key){regularSortDirection=regularSortDirection==='desc'?'asc':'desc'}else{regularSortKey=key;regularSortDirection='desc'}}render()}
function allNodes(nodes){let out=[];for(const node of nodes){out.push(node);if(node.children)out=out.concat(allNodes(node.children))}return out}
function nodeMatches(node){const own=includes([node.name,node.fileName,node.relativePath,node.type==='session'?node.session&&node.session.firstUser:''].join(' '));const child=(node.children||[]).some(nodeMatches);return own||child}
function render(){renderTree();renderContent()}
function renderProjectGroup(title,projects,key,direction,group){let html='';const rows=sortedProjectGroup(projects,key,direction).filter(project=>includes(project.name+' '+project.pathHint)||project.children.some(nodeMatches));if(!rows.length)return '';html+=`<div class="section toolbar"><span>${escapeHtml(title)}</span><div class="meta">${groupSortButtons(group,key,direction)}</div></div>`;for(const project of rows){const open=expanded.has(project.id)||Boolean(query);const updatedText=`${relTime(project.updated)}前`;html+=`<div class="item project ${open?'expanded':''} ${selectedNode&&selectedNode.id===project.id?'active':''}" data-id="${escapeHtml(project.id)}" data-type="project"><div class="title">${escapeHtml(project.name)}</div><div class="meta">${updatedText}</div><div class="sub">${escapeHtml(project.pathHint)}</div></div>`;if(open)html+=renderNodes(project.children,1,key,direction)}return html}
function renderTree(){let html='';const pinned=pinnedProjects();if(pinned.length)html+=renderProjectGroup('置顶项目',pinned,pinnedSortKey,pinnedSortDirection,'pinned');html+=renderProjectGroup('项目',regularProjects(),regularSortKey,regularSortDirection,'regular');el('tree').innerHTML=html||'<div class="small">没有匹配内容</div>'}
function renderNodes(nodes,level,key,direction){let html='';for(const node of sortedNodes(nodes,key,direction)){if(!nodeMatches(node))continue;const isFolder=node.type==='directory';const open=isFolder&&(expanded.has(node.id)||Boolean(query));const cls=isFolder?'folder':(node.type==='file'&&node.relativePath.startsWith('memory/')&&node.name.endsWith('.md')?'file memory-file':node.type);const active=selectedNode&&selectedNode.id===node.id?'active':'';const label=node.type==='session'?node.name:node.name;const meta=node.type==='directory'?'':relTime(node.updated)+'前';html+=`<div class="item ${cls} ${open?'expanded':''} ${active}" style="padding-left:${18+level*20}px" data-id="${escapeHtml(node.id)}" data-type="${node.type}"><div class="title">${escapeHtml(label)}</div><div class="meta">${meta}</div></div>`;if(open)html+=renderNodes(node.children||[],level+1,key,direction)}return html}
function findProject(id){return (data&&data.projects?data.projects:[]).find(project=>project.id===id||allNodes(project.children).some(node=>node.id===id))}
function findNode(id){for(const project of (data&&data.projects?data.projects:[])){if(project.id===id)return project;const found=allNodes(project.children).find(node=>node.id===id);if(found)return found}return null}
function renderContent(){if(!selectedNode){el('title').textContent='Claude 会话管理器';el('path').textContent='管理本地 '+data.root;el('stats').innerHTML='';el('chat').innerHTML='<div class="empty">选择左侧文件或会话查看内容</div>';return}if(selectedNode.children){el('title').textContent=selectedNode.name;el('path').textContent=selectedNode.pathHint||selectedNode.path;const rootProject=selectedRootProject();const pinButton=rootProject?`<button class="btn" onclick="togglePinProject()">${isProjectPinned(rootProject)?'取消置顶':'置顶'}</button>`:'';el('stats').innerHTML=`${pinButton}<button class="btn" onclick="copyPath()">复制路径</button><button class="btn" onclick="openInFinder()">在访达打开</button><button class="btn delete-btn" onclick="openDeleteModal()">删除</button>`;el('chat').innerHTML='<div class="empty">选择这个目录下的文件或会话查看内容</div>';return}if(selectedNode.type==='session'){renderSession(selectedNode.session);return}renderFile(selectedNode)}
function renderSession(session){el('title').textContent=session.title;el('path').textContent=session.cwd||session.file;el('stats').innerHTML=`<span class="pill">${session.messageCount} 条消息</span><button class="btn" onclick="copyPath()">复制路径</button><button class="btn" onclick="renameCurrentSession()">重命名</button><button class="btn" onclick="openInTerminal()">在 Claude 恢复</button><button class="btn delete-btn" onclick="openDeleteModal()">删除</button>`;let html='';for(const message of session.messages){let text=escapeHtml(message.text);text=text.replace(/```([\s\S]*?)```/g,'<pre>$1</pre>').replace(/\[工具调用\]|\[工具结果\]/g,x=>`<span class="tool">${x}</span>`);html+=`<div class="msg ${message.role==='user'?'user':'assistant'}"><div><div class="role">${message.role==='user'?'你':'Claude'} · ${message.time?new Date(message.time).toLocaleString():''}</div><div class="bubble">${text}</div></div></div>`}el('chat').innerHTML=html||'<div class="empty">这个会话没有可展示消息</div>';scrollChatToBottom()}
function renderFile(file){el('title').textContent=file.name;el('path').textContent=file.path;el('stats').innerHTML=`<span class="pill">${sizeText(file.size)}</span><span class="pill">${relTime(file.updated)}前</span><button class="btn" onclick="copyPath()">复制路径</button><button class="btn delete-btn" onclick="openDeleteModal()">删除</button>`;const reason=file.previewReason?`：${escapeHtml(file.previewReason)}`:'';const content=file.textAvailable?`<pre class="file-content">${escapeHtml(file.text)}</pre>`:`<div class="empty">这个文件暂不预览${reason}</div>`;el('chat').innerHTML=`<div class="file-view">${content}</div>`}
function scrollChatToBottom(){requestAnimationFrame(()=>{const chat=el('chat');chat.scrollTop=chat.scrollHeight})}
function selectItem(id,type){selectedNode=findNode(id);selectedProject=findProject(id);if(type==='project'||type==='directory'){if(expanded.has(id)){expanded.delete(id)}else{expanded.add(id)}}render()}
function selectedPath(){if(!selectedNode)return '';return selectedNode.type==='session'?selectedNode.session.file:(selectedNode.path||selectedNode.dir)}
function selectedKind(){if(!selectedNode)return '项目';if(selectedNode.type==='session')return '会话';if(selectedNode.type==='directory'||selectedNode.children)return '文件夹';return '文件'}
function selectedRootProject(){return selectedNode&&selectedProject&&selectedNode.id===selectedProject.id?selectedProject:null}
async function togglePinProject(){const project=selectedRootProject();if(!project){alert('请先选择一个项目');return}try{const response=await fetch('/api/pin-project',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({projectId:project.id,pinned:!isProjectPinned(project)})});let payload={};try{payload=await response.json()}catch{}if(!response.ok){alert(payload.error||'更新置顶状态失败');return}await reloadData(true)}catch{alert('更新置顶状态失败')}}
async function openDeleteModal(){if(!selectedNode){alert('请先在左侧选择要删除的项目、文件夹、文件或会话');return}const path=selectedPath();const kind=selectedKind();el('modalTitle').textContent=`确定删除这个${kind}吗？`;el('modalName').textContent=selectedNode.name||selectedNode.title;el('modalPath').textContent=path;el('modalFiles').style.display='none';el('modalFiles').textContent='';if(kind==='文件夹'){const response=await fetch('/api/list-files',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({path})});const payload=await response.json();if(!response.ok){alert(payload.error||'无法读取文件夹内容');return}el('modalFiles').style.display='block';if(payload.total){const prefix=payload.limited?`将一并移到废纸篓的文件：共 ${payload.total} 个，仅展示前 ${payload.files.length} 个：`:`将一并移到废纸篓的文件：共 ${payload.total} 个：`;el('modalFiles').textContent=`${prefix}\n${payload.files.join('\n')}`;}else{el('modalFiles').textContent='这个文件夹下没有文件';}}el('confirmModal').style.display='flex'}
function closeDeleteModal(){el('confirmModal').style.display='none'}
async function confirmTrash(){const path=selectedPath();const response=await fetch('/api/trash',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({path})});const payload=await response.json();if(!response.ok){alert(payload.error||'移动到废纸篓失败');return}closeDeleteModal();await reloadData()}
function copyPath(){navigator.clipboard.writeText(selectedPath())}
async function openInFinder(){if(!selectedNode||!selectedNode.children){alert('请先选择一个文件夹');return}const response=await fetch('/api/open-finder',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({path:selectedNode.path||selectedNode.dir})});const payload=await response.json();if(!response.ok){alert(payload.error||'无法在访达打开')}}
async function renameCurrentSession(){if(!selectedNode||selectedNode.type!=='session'){alert('请先选择一个会话');return}const title=prompt('输入新的会话名称：',selectedNode.session.title);if(title===null)return;const response=await fetch('/api/rename-session',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({path:selectedNode.session.file,title})});const payload=await response.json();if(!response.ok){alert(payload.error||'重命名失败');return}await reloadData(true)}
async function openInTerminal(){if(!selectedNode||selectedNode.type!=='session'){alert('请先选择一个会话');return}const response=await fetch('/api/open-terminal',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({path:selectedNode.session.file})});const payload=await response.json();if(!response.ok){alert(payload.error||'无法在终端打开')}}
async function reloadData(keepSelection=false){const previousId=selectedNode&&selectedNode.id;const payload=await fetch(authUrl('/api/data')).then(response=>response.json());data=payload;syncPreferences();selectedNode=null;selectedProject=null;if(keepSelection&&previousId){selectedNode=findNode(previousId);selectedProject=selectedNode?findProject(previousId):null}render()}
el('query').addEventListener('input',event=>{query=event.target.value;render()});
el('cancelDelete').addEventListener('click',closeDeleteModal);
el('confirmDelete').addEventListener('click',confirmTrash);
el('confirmModal').addEventListener('click',event=>{if(event.target.id==='confirmModal')closeDeleteModal()});
el('tree').addEventListener('click',event=>{const item=event.target.closest('.item');if(!item)return;selectItem(item.dataset.id,item.dataset.type)});
fetch('/api/open-page',{method:'POST',headers:{'Content-Type':'application/json'},body:authBody({clientId})});
window.addEventListener('pagehide',()=>navigator.sendBeacon('/api/close-page',new Blob([authBody({clientId})],{type:'application/json'})));
fetch(authUrl('/api/data')).then(response=>response.json()).then(payload=>{data=payload;syncPreferences();selectedProject=null;selectedNode=null;render()});
setInterval(()=>reloadData(true),60000);
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require_token(self, payload=None):
        query = parse_qs(urlparse(self.path).query)
        token = query.get("token", [None])[0]
        if payload is not None:
            token = payload.get("token") or token
        if token != AUTH_TOKEN:
            raise PermissionError("访问令牌无效，请从启动脚本打开页面")

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容过大")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json_body()
            self.require_token(payload)
            target = payload.get("path")
            project_id = payload.get("projectId")
            if path == "/api/open-page":
                client_id = payload.get("clientId")
                if client_id:
                    with PAGE_LOCK:
                        PAGE_CLIENTS.add(client_id)
                self.send_json(200, {"ok": True})
                return
            if path == "/api/pin-project":
                set_project_pinned(project_id, bool(payload.get("pinned")))
                self.send_json(200, {"ok": True})
                return
            if path == "/api/open-finder":
                open_path_in_finder(target)
                self.send_json(200, {"ok": True})
                return
            if path == "/api/close-page":
                schedule_shutdown_if_idle(payload.get("clientId"))
                self.send_json(200, {"ok": True})
                return
            if path == "/api/list-files":
                self.send_json(200, {"files": contained_files(target)})
                return
            if path == "/api/trash":
                trash_path(target)
                self.send_json(200, {"ok": True})
                return
            if path == "/api/open-terminal":
                open_session_in_terminal(target)
                self.send_json(200, {"ok": True})
                return
            if path == "/api/rename-session":
                rename_session(target, payload.get("title"))
                self.send_json(200, {"ok": True})
                return
            self.send_json(404, {"error": "接口不存在"})
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/data":
            try:
                self.require_token()
            except PermissionError as exc:
                self.send_json(403, {"error": str(exc)})
                return
            body = json.dumps(scan(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = HTML.replace("__AUTH_TOKEN__", AUTH_TOKEN).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="管理本地 Claude Code 项目和历史会话")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--json", action="store_true", help="只输出扫描结果，不启动网页服务")
    args = parser.parse_args()

    if args.json:
        print(json.dumps(scan(), ensure_ascii=False, indent=2))
        return

    if not is_loopback_host(args.host):
        raise SystemExit("为保护本地会话数据，Claude 会话管理器只能绑定 127.0.0.1、localhost 或 ::1")

    global SERVER
    SERVER = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Claude 会话管理器：http://{args.host}:{args.port}/?token={AUTH_TOKEN}")
    print(f"管理目录：{ROOT}")
    print(f"终端命令：{CLI_COMMAND}")
    print("关闭 Safari 页面后服务会自动退出")
    SERVER.serve_forever()


if __name__ == "__main__":
    main()

import os
import sys
import json
import sqlite3
import uuid
import threading
import time
import socket
import zipfile
import io
import re
import requests
from contextlib import contextmanager
from flask import Flask, render_template, request, Response, jsonify
from waitress import serve
import webview

# ================= 路径处理 (兼容 PyInstaller 打包) =================
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
    DB_FILE = os.path.join(os.path.expanduser("~"), "deepseek_chat_history.db")
    BASE_DIR = os.getcwd()
else:
    app = Flask(__name__, static_folder='static')
    DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.db")
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".deepseek_desktop_config.json")
PORT = int(os.environ.get("DEEPSEEK_PORT", "5000"))


# ================= 路径安全工具 =================
def is_within(base: str, target: str) -> bool:
    try:
        base_r = os.path.realpath(base)
        target_r = os.path.realpath(target)
        return os.path.commonpath([base_r, target_r]) == base_r
    except (ValueError, OSError):
        return False


# ================= SQLite 数据库 =================
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    try:
        with get_db() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
            ''')
            # 兼容旧库：为 messages 表补充 attachments 字段（存附件 JSON）
            cols = [r[1] for r in conn.execute('PRAGMA table_info(messages)').fetchall()]
            if 'attachments' not in cols:
                conn.execute('ALTER TABLE messages ADD COLUMN attachments TEXT')
    except Exception as e:
        print(f"⚠️ 数据库初始化失败: {e}")


init_db()


# ================= Flask 路由 =================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception:
            pass
    return jsonify({})


@app.route('/api/config', methods=['POST'])
def save_config():
    data = request.json or {}
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/balance', methods=['POST'])
def get_balance():
    data = request.json or {}
    api_key = data.get('api_key', '')
    api_url = data.get('api_url', 'https://api.deepseek.com/v1/chat/completions')

    if not api_key:
        return jsonify({"error": "请先设置 API 密钥"}), 400

    base_url = api_url.replace('/v1/chat/completions', '').rstrip('/')
    balance_url = f"{base_url}/user/balance"

    try:
        resp = requests.get(
            balance_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15
        )
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- 会话管理 ----
@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    with get_db() as conn:
        sessions = conn.execute('SELECT * FROM sessions ORDER BY updated_at DESC').fetchall()
    return jsonify([dict(s) for s in sessions])


@app.route('/api/sessions', methods=['POST'])
def create_session():
    data = request.json or {}
    session_id = data.get('id') or str(uuid.uuid4())
    title = (data.get('title') or '新对话')[:100]
    with get_db() as conn:
        conn.execute('INSERT INTO sessions (id, title) VALUES (?, ?)', (session_id, title))
    return jsonify({"id": session_id, "title": title})


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    with get_db() as conn:
        conn.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
    return jsonify({"status": "success"})


@app.route('/api/messages/<session_id>', methods=['GET'])
def get_messages(session_id):
    with get_db() as conn:
        msgs = conn.execute(
            'SELECT role, content, attachments FROM messages WHERE session_id = ? ORDER BY id ASC',
            (session_id,)
        ).fetchall()
    result = []
    for m in msgs:
        d = dict(m)
        if d.get('attachments'):
            try:
                d['attachments'] = json.loads(d['attachments'])
            except Exception:
                d['attachments'] = None
        result.append(d)
    return jsonify(result)


# ---- 对话主接口 ----
@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json or {}
    api_url = data.get('api_url', 'https://api.deepseek.com/v1/chat/completions')
    api_key = data.get('api_key', '')
    model = data.get('model', 'deepseek-chat')
    messages = data.get('messages', [])
    attachments = data.get('attachments', [])
    session_id = data.get('session_id')
    workspace = data.get('workspace', '')
    is_continue = bool(data.get('is_continue', False))

    if not api_key:
        return jsonify({"error": "请先设置 API 密钥"}), 400
    if not session_id:
        return jsonify({"error": "缺少会话 ID"}), 400
    if not messages:
        return jsonify({"error": "消息为空"}), 400

    # 工作区系统提示
    if workspace and os.path.isdir(workspace):
        sys_content = (
            "你是一个高级代码编写助手，具备自动写入本地文件的能力。\n"
            "当用户要求你生成或修改项目代码时，请**务必**遵循以下规则：\n"
            "1. 每个文件必须包裹在特定的代码块中，格式为：\n"
            "```[CREATE_FILE: 相对路径/文件名]\n"
            "文件内容...\n"
            "```\n"
            "2. 相对路径基于工作区根目录。例如 `src/main.py` 或 `index.html`。\n"
            "3. 不要使用普通的 ```python 语法。必须使用上面的 [CREATE_FILE] 标记，否则系统无法识别和保存。\n"
            "4. 如果你需要修改现有文件，请输出完整的文件内容并使用相同的标记覆盖。"
        )
        if is_continue:
            sys_content += (
                "\n5. 【续写模式】用户要求你继续上一次未完成的回复。"
                "请严格从上次中断处紧接着继续输出，规则："
                "不要重复任何已经输出过的文字、代码或注释；"
                "不要重新开头、不要总结、不要解释、不要输出任何说明性文字；"
                "若内容处于代码块内，直接接着写代码本身，"
                "不要添加新的注释，不要输出“继续”“以下是”等引导语。"
            )
        messages.insert(0, {"role": "system", "content": sys_content})

    user_msg = messages[-1]

    # 合并附件（仅非续写请求需要）
    if attachments and not is_continue:
        if isinstance(user_msg.get("content"), list):
            content_list = list(user_msg["content"])
        else:
            content_list = [{"type": "text", "text": user_msg.get("content", "")}]
        for att in attachments:
            if att.get('type') == 'image' and att.get('base64'):
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{att.get('mime', 'image/png')};base64,{att['base64']}"}
                })
            elif att.get('type') == 'text' and att.get('content'):
                content_list.append({
                    "type": "text",
                    "text": f"\n\n--- 附件: {att.get('name', 'attachment')} ---\n{att['content']}\n--- 附件结束 ---"
                })
        user_msg["content"] = content_list

    # 落库：仅非续写请求记录 user 消息
    inserted_user_msg = False
    if not is_continue:
        try:
            with get_db() as conn:
                count = conn.execute(
                    'SELECT COUNT(*) FROM messages WHERE session_id = ?', (session_id,)
                ).fetchone()[0]
                if count == 0:
                    raw = user_msg.get('content')
                    if isinstance(raw, list):
                        raw_text = next((c.get('text', '') for c in raw if c.get('type') == 'text'), '附件对话')
                    else:
                        raw_text = raw or ''
                    new_title = (raw_text[:20] + ('...' if len(raw_text) > 20 else '')) or '新对话'
                    conn.execute('UPDATE sessions SET title = ? WHERE id = ?', (new_title, session_id))

                # 纯文本内容
                raw_content = user_msg.get('content')
                if isinstance(raw_content, list):
                    plain_text = next((c.get('text', '') for c in raw_content if c.get('type') == 'text'), '')
                else:
                    plain_text = raw_content or ''

                # 附件信息序列化（存图片 base64 与文本附件内容，供历史还原）
                atts_json = json.dumps(attachments, ensure_ascii=False) if attachments else None

                conn.execute(
                    'INSERT INTO messages (session_id, role, content, attachments) VALUES (?, ?, ?, ?)',
                    (session_id, 'user', plain_text, atts_json)
                )
                conn.execute('UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (session_id,))
            inserted_user_msg = True
        except Exception as e:
            return jsonify({"error": f"写入数据库失败: {e}"}), 500

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "messages": messages, "temperature": 0.7, "stream": True}

    def cleanup_user_msg():
        if not inserted_user_msg:
            return
        try:
            with get_db() as conn:
                conn.execute(
                    'DELETE FROM messages WHERE id = ('
                    'SELECT id FROM messages WHERE session_id = ? AND role = "user" ORDER BY id DESC LIMIT 1)',
                    (session_id,)
                )
        except Exception as e:
            print(f"⚠️ 回滚用户消息失败: {e}")

    def persist_assistant(content: str):
        """非续写：INSERT；续写：UPDATE 最后一条 assistant"""
        try:
            with get_db() as conn:
                if is_continue:
                    last = conn.execute(
                        'SELECT id FROM messages WHERE session_id = ? AND role = "assistant" ORDER BY id DESC LIMIT 1',
                        (session_id,)
                    ).fetchone()
                    if last:
                        conn.execute('UPDATE messages SET content = ? WHERE id = ?', (content, last['id']))
                    else:
                        conn.execute(
                            'INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)',
                            (session_id, 'assistant', content)
                        )
                else:
                    conn.execute(
                        'INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)',
                        (session_id, 'assistant', content)
                    )
                conn.execute('UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (session_id,))
        except Exception as e:
            print(f"⚠️ 持久化 assistant 消息失败: {e}")

    def generate():
        full_reply = ""
        api_failed = False
        try:
            resp = requests.post(api_url, headers=headers, json=payload, stream=True, timeout=120)
            if resp.status_code != 200:
                api_failed = True
                yield f"data: {json.dumps({'error': f'API 错误 {resp.status_code}: {resp.text[:300]}'})}\n\n"
                return

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    json_data = json.loads(data_str)
                    delta = json_data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        full_reply += delta
                    yield f"data: {data_str}\n\n"
                except json.JSONDecodeError:
                    continue

            if full_reply:
                clean_reply = full_reply
                # 自动写文件（仅非续写请求执行，避免续写时误覆盖）
                if not is_continue and workspace and os.path.isdir(workspace):
                    abs_workspace = os.path.realpath(workspace)
                    pattern = re.compile(r"```\[CREATE_FILE:\s*(.*?)\]\s*\n(.*?)```", re.DOTALL)
                    matches = pattern.findall(full_reply)

                    for filename, content in matches:
                        filename = filename.strip()
                        if not filename:
                            continue
                        abs_target = os.path.realpath(os.path.join(workspace, filename))
                        if is_within(abs_workspace, abs_target):
                            try:
                                os.makedirs(os.path.dirname(abs_target), exist_ok=True)
                                with open(abs_target, 'w', encoding='utf-8') as f:
                                    f.write(content.strip())
                                print(f"✅ 自动创建/覆盖文件: {filename}")
                            except Exception as e:
                                print(f"⚠️ 写文件失败 {filename}: {e}")
                        else:
                            print(f"⛔ 拒绝越权写文件: {filename}")

                    clean_reply = pattern.sub(
                        lambda m: f"**已保存文件: {m.group(1)}**\n\n```\n{m.group(2).strip()}\n```",
                        full_reply
                    )

                persist_assistant(clean_reply)

        except Exception as e:
            api_failed = True
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if (api_failed or not full_reply) and not is_continue:
                cleanup_user_msg()

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


# ================= 工作区操作 API =================
@app.route('/api/workspace/select_dir', methods=['POST'])
def select_directory():
    try:
        if not webview.windows or len(webview.windows) == 0:
            return jsonify({"error": "目录选择仅支持桌面窗口环境"}), 400
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            return jsonify({"path": result[0]})
        return jsonify({"error": "未选择目录"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/workspace/files', methods=['GET'])
def get_workspace_files():
    workspace = request.args.get('path', '')
    try:
        max_depth = int(request.args.get('depth', '3'))
    except ValueError:
        max_depth = 3

    if not workspace or not os.path.isdir(workspace):
        return jsonify({"error": "工作区路径不存在"}), 400

    abs_workspace = os.path.realpath(workspace)
    file_tree = []
    try:
        for root, dirs, files in os.walk(abs_workspace):
            depth = root.replace(abs_workspace, '').count(os.sep)
            if depth > max_depth:
                dirs[:] = []
                continue
            for file in files:
                if file.endswith(('.py', '.js', '.html', '.css', '.json', '.txt', '.md',
                                  '.ts', '.jsx', '.tsx', '.yml', '.yaml')):
                    rel_path = os.path.relpath(os.path.join(root, file), abs_workspace)
                    file_tree.append(rel_path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"files": file_tree})


@app.route('/api/workspace/save', methods=['POST'])
def save_to_workspace():
    data = request.json or {}
    workspace = data.get('workspace')
    filepath = data.get('filepath')
    content = data.get('content')

    if not workspace or not filepath:
        return jsonify({"error": "缺少参数"}), 400
    if not os.path.isdir(workspace):
        return jsonify({"error": "工作区不存在"}), 400

    abs_workspace = os.path.realpath(workspace)
    abs_target = os.path.realpath(os.path.join(workspace, filepath))

    if not is_within(abs_workspace, abs_target):
        return jsonify({"error": "非法路径"}), 403

    try:
        os.makedirs(os.path.dirname(abs_target) or abs_workspace, exist_ok=True)
        with open(abs_target, 'w', encoding='utf-8') as f:
            f.write(content or '')
        return jsonify({"status": "success", "path": abs_target})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/workspace/delete', methods=['POST'])
def delete_workspace_file():
    data = request.json or {}
    workspace = data.get('workspace')
    filepath = data.get('filepath')

    if not workspace or not filepath:
        return jsonify({"error": "缺少参数"}), 400
    if not os.path.isdir(workspace):
        return jsonify({"error": "工作区不存在"}), 400

    abs_workspace = os.path.realpath(workspace)
    abs_target = os.path.realpath(os.path.join(workspace, filepath))

    if not is_within(abs_workspace, abs_target):
        return jsonify({"error": "非法路径"}), 403

    try:
        if os.path.isfile(abs_target):
            os.remove(abs_target)
            return jsonify({"status": "success"})
        return jsonify({"error": "文件不存在"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- 打包项目源码 ----
@app.route('/api/download_project')
def download_project():
    root_files = ["app.py", "requirements.txt", "build.sh", "build.bat"]
    dirs_to_zip = ["templates", "static"]

    memory_file = io.BytesIO()
    try:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in root_files:
                full_path = os.path.join(BASE_DIR, f)
                if os.path.exists(full_path):
                    zf.write(full_path, f)
            for d in dirs_to_zip:
                dir_path = os.path.join(BASE_DIR, d)
                if os.path.exists(dir_path):
                    for root, _, files in os.walk(dir_path):
                        for file in files:
                            filepath = os.path.join(root, file)
                            arcname = os.path.relpath(filepath, BASE_DIR)
                            zf.write(filepath, arcname)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    memory_file.seek(0)
    return Response(
        memory_file.read(),
        mimetype='application/zip',
        headers={'Content-Disposition': 'attachment; filename=deepseek_web_source.zip'}
    )


# ================= 桌面启动 =================
class ServerThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        serve(app, host='127.0.0.1', port=PORT, _quiet=True)


def wait_for_server(host='127.0.0.1', port=None, timeout=10):
    port = port or PORT
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Api:
    def save_file(self, content, filename):
        if not filename or filename.endswith('.undefined') or filename.endswith('.null'):
            filename = 'code_snippet.txt'
        try:
            result = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, save_filename=filename
            )
        except Exception as e:
            print(f"打开保存对话框失败: {e}")
            return False
        if not result:
            return False
        file_path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        except Exception as e:
            print(f"保存文件失败: {e}")
            return False

    def copy_text(self, content):
        """通过 pywebview 写入系统剪贴板（作为前端 Clipboard API 的兜底）"""
        import pyperclip
        try:
            pyperclip.copy(content or '')
            return True
        except Exception as e:
            print(f"复制到剪贴板失败: {e}")
            return False

    def save_image(self, data_url, filename):
        """保存 base64 dataURL 图片到本地（通过系统保存对话框）"""
        import base64 as b64
        if not filename or filename.endswith('.undefined') or filename.endswith('.null'):
            filename = 'image.png'
        try:
            result = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, save_filename=filename
            )
        except Exception as e:
            print(f"打开保存对话框失败: {e}")
            return False
        if not result:
            return False
        file_path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            # 解析 dataURL: data:image/png;base64,xxxx
            header, _, data = (data_url or '').partition(',')
            if ';base64' not in header:
                print("图片数据格式无效")
                return False
            raw = b64.b64decode(data)
            with open(file_path, 'wb') as f:
                f.write(raw)
            return True
        except Exception as e:
            print(f"保存图片失败: {e}")
            return False


def on_closing():
    time.sleep(0.1)
    os._exit(0)


if __name__ == '__main__':
    server_thread = ServerThread()
    server_thread.start()

    if not wait_for_server(timeout=15):
        print("❌ 本地服务启动失败")
        sys.exit(1)

    api = Api()
    window = webview.create_window(
        title="Shawro AI",
        url=f"http://127.0.0.1:{PORT}",
        width=1200, height=800,
        min_size=(800, 600),
        background_color='#18181b',
        easy_drag=False,
        js_api=api,
    )
    window.events.closing += on_closing
    webview.start()
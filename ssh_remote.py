#!/usr/bin/env python3
"""远端只读探针：读取 SSH 服务器上 Codex / Claude Code 的会话记录，输出一行 JSON。

由本地 collector 用 `ssh <host> 'python3 - <host> <window> <max>' < ssh_remote.py`
送入执行（脚本从 stdin 读，参数从命令行来）；远端不落任何文件。也可以在本机
`./ssh_remote.py <标签> [窗口秒数] [最多条数]` 直接跑一遍做自检。

只读约束：不写、不建、不删远端文件，不执行 shell 启动脚本，不读 auth.json /
settings.json / .claude.json 等凭据，也不输出转写正文，只给标题、提问预览
（脱敏后截断）和状态时间。纯标准库，py3.8+。
"""
from contextlib import closing
from pathlib import Path
import glob
import json
import os
import re
import sys
import time

WINDOW = 24 * 3600
STALE = 30 * 60
# 这些 stop_reason 说明这一轮是异常结束的，不是正常完成。
FAILED_REASONS = {'refusal', 'max_tokens', 'model_context_window_exceeded'}
RECORD_FRESH = 5 * 60        # 注册表 5 分钟内被写过，说明这个进程还活着
TITLE_LIMIT = 50
PROMPT_LIMIT = 120
TAIL_BUDGET = 4 * 1024 * 1024      # 转写可达数十 MB，只解析尾部

# 界面注入的包装块，不是用户真正问的问题。
WRAPPER_BLOCK = re.compile(r'<(system-reminder|environment_context|INSTRUCTIONS|command-name|command-message|'
                           r'command-args|local-command-stdout|local-command-caveat|user-prompt-submit-hook|'
                           r'turn_aborted|task-notification|codex_apps_client_time_context|fork-boilerplate)\b[^>]*>.*?</\1>', re.S)
LONG_PATH = re.compile(r'(?<![\w.])(?:~|/)(?:[^\s/]+/){2,}([^\s/]+/?)')
SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')
INTERRUPT_MARKERS = ('[Request interrupted by user', '[Request cancelled by user', '[Request aborted')
CLAUDE_RUNNING = ('busy', 'working', 'running', 'compacting', 'starting')
CLAUDE_WAITING = ('permission', 'waiting', 'blocked')
USER_NOISE_PREFIXES = ('<', '# AGENTS.md', '# CLAUDE.md', 'Caveat:', 'This session is being continued',
                       'Base directory for this skill', 'Continue from where you left off')
# Rows Claude Code writes by itself (compaction summary, skill/command expansion): not your prompts.
CLAUDE_NOISE_FLAGS = ('isMeta', 'isCompactSummary', 'isVisibleInTranscriptOnly', 'isSynthetic')


def one_line(text, limit=80):
    text = SECRET.sub('[密钥已隐藏]', str(text or ''))
    text = re.sub(r'(?m)^\s*(#+|>+|[-*]\s)\s*', '', text)
    text = re.sub(r'\*\*|__|`', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def prompt_preview(text, limit=PROMPT_LIMIT):
    text = WRAPPER_BLOCK.sub(' ', str(text or '')).strip()
    if not text or text.startswith(USER_NOISE_PREFIXES):
        return ''
    if text.startswith(INTERRUPT_MARKERS):
        return ''
    if text.startswith('/goal'):
        text = text[5:].strip()
    text = re.sub(r'\\([*_`#>\[\]()~-])', r'\1', text)
    text = LONG_PATH.sub(r'…/\1', text)
    return one_line(text, limit)


def stamp(value):
    """Seconds since epoch. Accepts epoch seconds/millis and RFC 3339 strings."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        if value <= 0:
            return 0
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0
        try:
            return stamp(float(value))
        except ValueError:
            pass
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return 0
    return 0


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get('type') in ('text', 'input_text'):
                parts.append(str(part.get('text', '')))
        return ' '.join(parts)
    return ''


# Keep this in step with collector.py: local and server Codex records use the
# same image-upload wrapper, and the task panel should render them identically.
CODEX_IMAGE_PLACEHOLDER = '【已上传图片】'
CODEX_FILES_REQUEST = re.compile(
    r'(?ms)^\s*# Files mentioned by the user:\s*.*?^\s*## My request:\s*(.*)$')
CODEX_IMAGE_TAG = re.compile(r'<image\s+name=[^>]*>', re.I | re.S)
CODEX_IMAGE_LABEL = re.compile(r'^\s*\[Image\s*#\d+\]\s*', re.I)


def codex_image_content(content, raw=None):
    """Whether a Codex message contains an uploaded image."""
    raw = text_content(content) if raw is None else str(raw or '')
    if isinstance(content, list) and any(
        isinstance(part, dict) and part.get('type') in ('input_image', 'local_image')
        for part in content
    ):
        return True
    return bool(re.search(
        r'(?ms)^\s*# Files mentioned by the user:\s*.*?\bImage attachment:\s*true\b'
        r'.*?^\s*## My request:', raw))


def codex_prompt_preview(content):
    """Show a Codex prompt without the image upload wrapper or local path."""
    raw = text_content(content)
    if not codex_image_content(content, raw):
        return prompt_preview(raw)
    found = CODEX_FILES_REQUEST.search(raw)
    request = found.group(1) if found else raw
    request = CODEX_IMAGE_TAG.sub(' ', request)
    request = CODEX_IMAGE_LABEL.sub('', request)
    request = prompt_preview(request)
    return (request + ' ' + CODEX_IMAGE_PLACEHOLDER).strip() if request else CODEX_IMAGE_PLACEHOLDER


def json_lines(stream):
    for line in stream:
        if not line.endswith('\n'):
            continue        # a writer may be halfway through its last record
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict):
            yield row


def _read_tail(path, budget):
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        if size > budget:
            f.seek(size - budget)
        data = f.read()
    lines = data.split(b'\n')
    if size > budget:
        lines = lines[1:]       # drop the partial first line
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def tail_rows(path, budget=TAIL_BUDGET, codex=False):
    """Parse the tail of a transcript, widening the window when the start is missing.

    A long-running Codex thread writes a lot of token counts after its last
    task_started; a fixed tail can end up containing no turn boundaries at all,
    which made the whole session disappear from the panel. Keep doubling the
    window until an event that opens a turn shows up, or the file is exhausted.
    """
    size = os.path.getsize(path)
    limit = min(size, budget)
    missing = None
    while True:
        rows = _read_tail(path, limit)
        if limit >= size:
            return rows
        if any(_OPENS_TURN(row) for row in rows):
            if not codex:
                return rows
            # The previous turn may end in the same second a Goal resumes.
            # Fetch its opening as well, so we know which task it belongs to.
            events = [row['payload'] for row in rows if row.get('type') == 'event_msg'
                      and isinstance(row.get('payload'), dict)]
            opened = {p.get('turn_id') for p in events if p.get('type') == 'task_started'}
            if missing is None:
                missing = {p['turn_id'] for p in events if p.get('type') == 'task_complete' and p.get('turn_id')} - opened
            if missing.issubset(opened):
                if not missing:
                    return rows
                # Keep lifecycle metadata, but exclude partial turns newly
                # exposed at the beginning of the expanded window.
                for i, row in enumerate(rows):
                    p = row.get('payload') or {}
                    if (row.get('type') == 'event_msg' and p.get('type') == 'task_started'
                            and p.get('turn_id') in missing):
                        metadata = [r for r in rows[:i] if r.get('type') == 'session_meta' or
                                    (r.get('type') == 'event_msg' and (r.get('payload') or {}).get('type') == 'thread_goal_updated')]
                        return metadata + rows[i:]
        limit = min(size, limit * 2)


def _OPENS_TURN(row):
    """An event that truly *starts* a turn, or names a thread.

    These deliberately do not count:

    - Codex's `task_complete`. After a compact the ending sits far from the
      beginning; a tail holding only the ending would look complete and the
      question (written at task_started) would be lost.
    - Claude's `tool_result` rows. They are `type: user` but they are tool output
      echoing back, not something you typed. A long file can have tens of
      thousands of them and no real prompt, so treating them as an opening
      stops the window from widening right where the question is missing.
    - Claude's `assistant` rows. Replies come after the question; a tail full of
      tool loops has plenty of them and still no question, so they cannot stand
      in for the start of an exchange either.
    - Claude rows that Claude Code wrote itself, above all the summary it writes
      after compacting the context (`isCompactSummary`, text starting with
      “This session is being continued”). It is a `type: user` row, so it used
      to end the search — and `claude_tail` then skips it as noise, leaving the
      panel with no question at all for exactly that exchange.
    - Stop markers. `[Request interrupted by user]` closes an exchange; the
      question it belonged to is written before it.
    """
    typ = row.get('type')
    if typ == 'session_meta':
        return True
    if typ == 'event_msg':
        return (row.get('payload') or {}).get('type') == 'task_started'
    if typ == 'attachment':
        return bool(queued_prompt(row))     # a message you typed while Claude was working
    if typ == 'user':
        content = (row.get('message') or {}).get('content')
        if isinstance(content, list) and any(
                isinstance(part, dict) and part.get('type') == 'tool_result' for part in content):
            return False        # tool output, not a question
        if any(row.get(flag) for flag in CLAUDE_NOISE_FLAGS):
            return False        # compaction summary, skill / command expansion
        return bool(prompt_preview(text_content(content)))
    return False


# Claude 的转写同理：一段很长的工具调用之后，尾部可能没有开场记录。



def new_turn(tid, start=0):
    return {'id': str(tid), 'start': start, 'end': 0, 'state': 'running', 'detail': '',
            'prompt': '', 'resumed': False, 'queued': False, 'user_interrupted': False}


FOLLOWUPS_KEPT = 2


def keep_first_prompt(turn, value):
    """First prompt names the task; later ones in the same turn are follow-ups (追加)."""
    if not value:
        return
    if not turn.get('prompt') or turn.get('prompt_inherited'):
        turn['prompt'] = value
        turn['prompt_inherited'] = False
        return
    seen = turn.setdefault('seen_prompts', set())
    if value == turn['prompt'] or value in seen:
        return              # Codex logs each message twice
    seen.add(value)
    turn['followups'] = (turn.get('followups', []) + [value])[-FOLLOWUPS_KEPT:]
    turn['followup_count'] = turn.get('followup_count', 0) + 1


def add_followups(item, followups, count):
    if followups:
        item['followups'] = list(followups)
        item['followupCount'] = max(count, len(followups))
    return item


def error_text(error):
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or ('未知错误' if error else ''))
    return str(error or '')


# ---------------------------------------------------------------- Codex

def codex_thread_id(path, found=''):
    """Thread id for a rollout file.

    Normally it comes from session_meta. After a compact the file can grow until
    the metadata line falls outside the window we read, so fall back to the UUID
    in the file name, and finally to the name itself: losing the id would make
    the whole session disappear from the panel.
    """
    if found:
        return found
    name = os.path.basename(path)
    match = re.search(r'rollout-\d{4}-\d{2}-\d{2}T[\d-]+-([0-9a-f-]{36})', name)
    if match:
        return match.group(1)
    match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', name)
    if match:
        return match.group(1)
    return os.path.splitext(name)[0]


def read_codex_goals(root):
    """Read one snapshot per poll, independently of the transcript cache."""
    try:
        import sqlite3
    except ImportError:
        return {}
    path = (Path(root) / 'goals_1.sqlite').absolute()
    try:
        # With no WAL, immutable mode avoids creating journal sidecars. An
        # existing WAL needs its shared index to be readable without setup.
        wal = Path(str(path) + '-wal').exists()
        if wal and not Path(str(path) + '-shm').exists():
            return {}
        uri = path.as_uri() + ('?mode=ro' if wal else '?mode=ro&immutable=1')
        with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as db:
            rows = db.execute('SELECT thread_id, goal_id, objective, status, created_at_ms, updated_at_ms FROM thread_goals')
            return {sid: dict(id=gid, objective=prompt_preview(obj), status=status,
                              created=created / 1000, updated=updated / 1000)
                    for sid, gid, obj, status, created, updated in rows}
    except (OSError, sqlite3.Error):
        return {}


def record_goal_update(info, data, ts):
    if not isinstance(data, dict):
        return prompt_preview(data)
    objective = prompt_preview(data.get('objective'))
    snapshots = info.setdefault('goals', {})
    created = stamp(data.get('createdAt'))
    if not created:
        prior = next(reversed(snapshots.values()), {})
        created = prior.get('created') if prior.get('objective') == objective else ts
    snapshots[created] = dict(objective=objective, status=data.get('status'), created=created,
                              updated=stamp(data.get('updatedAt')) or ts)
    return objective


def codex_goal_turns(info, stored):
    """Project Goal checkpoints into a task. Leave ordinary turns intact."""
    turns = info['turns']
    if not any(t.get('goal') for t in turns.values()):
        return turns
    snapshots = dict(info.get('goals', {}))
    if stored:
        # Rollouts round creation to seconds. The store also captures status
        # changes made between turns, without a corresponding rollout event.
        snapshots.pop(int(stored['created']), None)
        snapshots[stored['created']] = stored
    history = sorted(snapshots.values(), key=lambda g: g['created'], reverse=True)
    result, groups = {}, {}
    for t in turns.values():
        if not t.get('goal'):
            result[t['id']] = t
            continue
        opened = t.get('goal_at') or t.get('opened_at') or t['start']
        goal = next((g for g in history if g['created'] <= opened), {})
        objective = goal.get('objective') or t.get('objective') or t['prompt']
        key = goal.get('id') or goal.get('created') or objective or t['id']
        group = groups.setdefault(key, [])
        group.append((t, goal))
    for group in groups.values():
        first = group[0][0]
        t, goal = max(reversed(group), key=lambda pair: pair[0].get('opened_at') or pair[0]['start'])
        state = goal.get('status')
        if t.get('user_interrupted') or state == 'paused':
            continue
        goal_id = 'goal:' + str(goal.get('id') or first['id'])
        label = goal.get('objective') or t.get('objective') or first.get('objective') or t['prompt'] or 'Codex 目标'
        task = dict(t, id=goal_id, prompt=label, prompt_inherited=True,
                    goal_start=goal.get('created') or first['start'])
        # An async question can outlive its turn. Its matching user reply has
        # already cleared it in the parser.
        asks = {k: a for old, _ in group for k, a in old.get('asks', {}).items() if k.startswith('qa:')}
        asks.update(t.get('asks', {}))
        task['asks'] = asks
        if state == 'complete':
            task.update(end=goal['updated'] or t['end'], state='success', asks={},
                        detail=t['detail'] if t['state'] == 'success' else '目标已完成')
        elif t['state'] not in ('error', 'interrupted'):
            if state in ('blocked', 'budget_limited', 'usage_limited'):
                reason = {'blocked': '目标遇到阻碍', 'budget_limited': '目标已达到预算上限',
                          'usage_limited': '目标已达到用量上限'}[state]
                task.update(end=goal['updated'], state='unknown', detail=reason)
            elif t['end'] and state == 'active':
                task.update(end=0, state='running', detail='等待 Codex 继续执行目标')
            elif t['end'] and not asks:
                task.update(state='unknown', detail='本轮已结束，目标状态待确认')
        result[goal_id] = task
    return result


def parse_codex(path):
    """Parse one Codex rollout file (server side, codex CLI).

    Deliberately duplicated in collector.py (local side, ChatGPT desktop). The two
    apps share a file format but are not the same: originator is codex_work_desktop
    vs codex-tui, the desktop channel ships alpha builds and has goal mode. Keep
    the two in step by hand — a fix here usually belongs there too.
    """
    info = {'id': '', 'title': '', 'skip': False, 'turns': {}, 'last': 0}
    goal = prompt = ''
    latest = previous = None
    for row in tail_rows(path, codex=True):
        p = row.get('payload')
        if not isinstance(p, dict):
            p = {}
        ts = stamp(row.get('timestamp'))
        if ts > info['last']:
            info['last'] = ts
        typ = row.get('type')
        if typ == 'session_meta' and not info['id']:
            info['id'] = p.get('id') or p.get('session_id') or ''
            source = json.dumps(p.get('thread_source') or p.get('source') or '').lower()
            info['skip'] = bool(p.get('parent_thread_id')) or any(s in source for s in ('subagent', 'sub_agent', 'spawn'))
        codex_question_rows(row, info['turns'], latest)
        if typ == 'response_item':
            # goal 模式下，你设的目标不会写成 UserMessage，只在这里出现。
            p = row.get('payload') or {}
            if p.get('type') == 'message' and p.get('role') == 'user':
                value = goal_prompt(text_content(p.get('content')))
                if value:
                    meta = p.get('internal_chat_message_metadata_passthrough') or {}
                    tid = meta.get('turn_id')
                    turn = info['turns'].get(tid) if tid else latest
                    if turn is not None and not turn['end']:
                        turn['goal'] = True
                        turn['objective'] = value
                        turn['goal_at'] = ts
                        if not turn['prompt']:
                            turn['prompt'] = value
                    goal = value
            continue
        if typ != 'event_msg':
            continue
        ev, tid = p.get('type'), p.get('turn_id')
        if ev in ('task_started', 'task_complete') and tid:
            previous = latest
            latest = info['turns'].setdefault(tid, new_turn(tid))
            latest['start'] = latest['start'] or stamp(p.get('started_at')) or ts
            latest['queued'] = False        # it has really started now
            if ev == 'task_started':
                latest['opened_at'] = ts or latest['start']
                if (p.get('turn_attribution') or {}).get('turn_trigger') == 'goal':
                    latest['goal'] = True
                    latest['objective'] = goal
            if ev == 'task_started' and (p.get('turn_attribution') or {}).get('turn_trigger') == 'resume_interrupted_task':
                latest['resumed'] = True
                if not latest['prompt'] and previous is not None and previous is not latest and previous['prompt']:
                    latest['prompt'] = previous['prompt']
                    latest['prompt_inherited'] = True
            elif ev == 'task_complete':
                latest['end'] = stamp(p.get('completed_at')) or ts
                latest['state'] = 'error' if p.get('error') else 'success'
                latest['detail'] = one_line(error_text(p.get('error')) or p.get('last_agent_message') or '已完成')
        elif ev in ('turn_aborted', 'task_aborted'):
            turn = info['turns'].get(tid) if tid else latest
            if turn:
                user_stop = ev == 'turn_aborted' or p.get('reason') in ('user', 'cancelled', 'canceled')
                turn.update(end=ts, state='interrupted', detail='已中断',
                            user_interrupted=bool(turn.get('user_interrupted') or user_stop))
        elif ev == 'item_completed':
            item = p.get('item') or {}
            if item.get('type') == 'UserMessage':
                raw = text_content(item.get('content'))
                value = question_reply(raw) or codex_prompt_preview(item.get('content'))
                if value:
                    clear_async_questions(info['turns'])
                prompt = prompt or value
                turn = codex_turn_for(info['turns'], tid, latest, ts)
                if turn:
                    keep_first_prompt(turn, value)
        elif ev == 'user_message':
            value = question_reply(p.get('message')) or codex_prompt_preview(p.get('message'))
            if value:
                clear_async_questions(info['turns'])
            prompt = prompt or value
            turn = codex_turn_for(info['turns'], tid, latest, ts)
            if turn:
                keep_first_prompt(turn, value)
        elif ev == 'thread_goal_updated':
            goal = record_goal_update(info, p.get('goal'), ts)
        elif ev == 'thread_name_updated':
            info['title'] = one_line(p.get('thread_name'), TITLE_LIMIT) or info['title']
    info['id'] = codex_thread_id(path, info['id'])
    info['title'] = info['title'] or one_line(goal, TITLE_LIMIT) or one_line(prompt, TITLE_LIMIT) or 'Codex 任务'
    return info


def codex_titles(home):
    titles = {}
    path = os.path.join(home, '.codex', 'session_index.jsonl')
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for row in json_lines(f):
                name = one_line(row.get('thread_name'), TITLE_LIMIT)
                if row.get('id') and name:
                    titles[row['id']] = name
    except OSError:
        pass
    return titles


def codex_running(home):
    try:
        return os.path.getsize(os.path.join(home, '.codex', 'app-server-daemon', 'daemon.pid')) > 0
    except OSError:
        return False


def collect_codex(home, now, window, host):
    titles = codex_titles(home)
    goals = read_codex_goals(Path(home) / '.codex')
    cutoff = now - window
    files = []
    for path in glob.glob(os.path.join(home, '.codex', 'sessions', '*', '*', '*', 'rollout-*.jsonl')):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue        # old sessions become eligible again after a new write
        files.append((st.st_mtime, path))
    files.sort(reverse=True)
    items, status = [], 'ok'
    for mtime, path in files:
        try:
            info = parse_codex(path)
        except (OSError, ValueError):
            status = 'partial'
            continue
        if info.get('skip') or not info.get('id') or not info['turns']:
            continue
        info['title'] = titles.get(info['id'], info['title'])
        last = max(info['last'], mtime)
        turns = codex_goal_turns(info, goals.get(info['id']))
        if not turns:
            continue
        started = [t for t in turns.values() if not t.get('queued')]
        latest = max(started or turns.values(), key=lambda t: (t['start'], t.get('opened_at', 0)))
        # Turns you started yourself (not Codex resuming on its own), by when you asked.
        prompt_times = [t['start'] for t in turns.values() if t.get('prompt') and not t.get('prompt_inherited')]
        for turn in turns.values():
            # A newer turn supersedes an older interrupted/unknown one.
            if turn is not latest and not turn.get('queued') and (not turn['end'] or turn['state'] in ('interrupted', 'unknown')):
                continue
            if turn.get('user_interrupted'):
                continue
            # You asked again in this thread after it finished: you have seen it (as for Claude here).
            if turn['end'] and any(a > turn['end'] for a in prompt_times):
                continue
            item = {'agent': 'chatgpt', 'host': host, 'session': info['id'], 'title': info['title'],
                    'prompt': turn.get('prompt', ''), 'start': turn.get('goal_start') or turn.get('start') or last,
                    'resumed': bool(turn.get('resumed'))}
            if turn.get('goal'):
                item['turn'] = turn['id']
            asks = turn.get('asks') or {}
            blocking = [a for k, a in asks.items() if k.startswith('q:')]
            pending_async = [a for k, a in asks.items() if k.startswith('qa:')]
            if blocking and not turn['end']:
                # request_user_input: the turn waits for your answer; quiet is expected, never stale.
                detail, asked = max(blocking, key=lambda a: a[1])
                item.update(state='question', detail=detail, time=asked)
            elif pending_async and (not turn['end'] or turn['state'] == 'success'):
                # request_user_input_async: open until you write in the thread, even after the turn ends.
                detail, asked = max(pending_async, key=lambda a: a[1])
                item.update(state='question', detail=detail, time=turn['end'] or asked)
            elif turn['end']:
                item.update(state=turn['state'], detail=turn.get('detail', ''), time=turn['end'])
            elif now - last < STALE:
                item.update(state='running', detail='已排队，等待开始' if turn.get('queued') else turn.get('detail', ''), time=last)
            else:
                item.update(state='unknown', time=last,
                            detail='超过 30 分钟没有新进展，请检查服务器上的任务')
            if item['time'] and now - item['time'] > window:
                continue
            items.append(add_followups(item, turn.get('followups'), turn.get('followup_count', 0)))
    return {'status': status, 'machine_hint': codex_running(home), 'items': items}


# ---------------------------------------------------------------- Claude Code

def claude_session_id(path, rows):
    for row in rows:
        if isinstance(row.get('sessionId'), str) and row['sessionId']:
            return row['sessionId']
    base = os.path.basename(path)
    return base[:-6] if base.endswith('.jsonl') else base


def queued_prompt(row):
    """A message typed while Claude was working: Claude Code stores it as a
    queued_command attachment, not as a user row. Background-task notices use the
    same attachment with another commandMode; those are not your prompts."""
    att = row.get('attachment') if isinstance(row.get('attachment'), dict) else {}
    if att.get('type') != 'queued_command' or att.get('commandMode') not in (None, '', 'prompt'):
        return ''
    return prompt_preview(text_content(att.get('prompt')))


QUESTION_TOOLS = {'AskUserQuestion': '等你回答', 'ExitPlanMode': '等你确认计划',
                  # Codex: request_user_input blocks until answered; the _async one returns at
                  # once and stays open until your reply message arrives.
                  'request_user_input': '等你回答', 'request_user_input_async': '等你回答'}
QUESTION_REPLY = re.compile(r'<send_user_message_question_reply>(.*?)</send_user_message_question_reply>', re.S)


def question_detail(name, args):
    """Panel text for a question tool call (AskUserQuestion / ExitPlanMode / Codex), else ''."""
    lead = QUESTION_TOOLS.get(name)
    if not lead:
        return ''
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    qs = args.get('questions') if isinstance(args, dict) else None
    qs = [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []
    first = one_line(qs[0].get('question') or qs[0].get('title') or qs[0].get('header'), 60) if qs else ''
    count = ' %d 个问题' % len(qs) if len(qs) > 1 else ''
    return lead + count + ('：' + first if first else '')


# Codex 的 goal 模式：你设的目标不是普通 UserMessage，而是包在这个标签里的
# response_item。source="user_goal" 是你设目标那一次，source="goal" 是之后自动续跑。
GOAL_CONTEXT = re.compile(r'<codex_internal_context[^>]*source="(?:user_goal|goal)"[^>]*>(.*?)</codex_internal_context>', re.S)
GOAL_OBJECTIVE = re.compile(r'<objective>(.*?)</objective>', re.S)
GOAL_SET = re.compile(r'User set the goal:\s*"(.+?)"\s*(?:\n|$)', re.S)


def goal_prompt(text):
    """The goal text from a codex_internal_context block, or '' if this is not one."""
    found = GOAL_CONTEXT.fullmatch(str(text or '').strip())
    if not found:
        return ''
    body = found.group(1)
    objective = GOAL_OBJECTIVE.search(body)
    if objective:
        return prompt_preview(objective.group(1))
    set_goal = GOAL_SET.search(body)
    if set_goal:
        return prompt_preview(set_goal.group(1).replace('\\"', '"'))
    return ''


def question_reply(text):
    """Your answer to a Codex async question, shown as "回答：…"; '' if this is not one."""
    found = QUESTION_REPLY.search(str(text or ''))
    if not found:
        return ''
    try:
        replies = json.loads(found.group(1))
    except ValueError:
        return '回答'
    answers = [one_line(r.get('answer'), 60) for r in replies if isinstance(r, dict)] if isinstance(replies, list) else []
    return '回答：' + '；'.join(a for a in answers if a) if any(answers) else '回答'


def codex_turn_for(turns, tid, latest, ts):
    """The turn a message belongs to, created if Codex has not opened it yet.

    Normally `task_started` comes first and the turn already exists. But when your
    prompt makes Codex compact the thread first, the message is written with its
    `turn_id` while the turn itself only starts after the compaction: looking the
    id up returned nothing and the question was dropped, so the panel showed the
    task with no prompt. Treat it as queued until its `task_started` arrives.
    """
    if not tid:
        return latest
    turn = turns.get(tid)
    if turn is None:
        turn = turns[tid] = new_turn(tid, ts)
        turn['queued'] = True
    return turn


def codex_question_rows(row, turns, latest):
    """Codex question tools are response items: open on the call; the blocking one closes
    on its output, the async one when you next write in the thread."""
    if row.get('type') != 'response_item':
        return
    p = row.get('payload') or {}
    kind, call = p.get('type'), p.get('call_id')
    if not call:
        return
    if kind in ('function_call', 'custom_tool_call'):
        name = str(p.get('name') or '')
        detail = question_detail(name, p.get('arguments') if kind == 'function_call' else p.get('input'))
        meta = p.get('internal_chat_message_metadata_passthrough') or {}
        t = turns.get(meta.get('turn_id')) or latest
        if detail and t is not None:
            prefix = 'qa:' if name.endswith('_async') else 'q:'
            t.setdefault('asks', {})[prefix + str(call)] = (detail, stamp(row.get('timestamp')))
    elif kind in ('function_call_output', 'custom_tool_call_output'):
        for t in turns.values():
            t.get('asks', {}).pop('q:' + str(call), None)


def clear_async_questions(turns):
    for t in turns.values():
        for key in [k for k in t.get('asks', {}) if k.startswith('qa:')]:
            t['asks'].pop(key)


def claude_tail(rows):
    """Where the last exchange stands, from the tail of one transcript.

    An exchange runs from the first real prompt after Claude last finished
    (end_turn or interrupt) to the next finish. Prompts typed while Claude is
    still working belong to the same exchange: Claude answers them together, so
    they are one task named by the first prompt, with the rest as follow-ups.
    """
    last_prompt = last_reply = last_row = 0
    end_turn = interrupt = failure = 0
    user_interrupted = False
    replies = []
    current = None                          # exchange in flight: {'prompt', 'start', 'followups', ...}
    done = None                             # last finished exchange
    questions = {}                          # tool_use id -> (detail, ts): asked you, not answered yet
    for row in rows:
        typ = row.get('type')
        if row.get('isSidechain') or row.get('parent_tool_use_id'):
            continue
        ts = stamp(row.get('timestamp'))
        if typ == 'attachment':
            value = queued_prompt(row)
            if value:
                last_row = max(last_row, ts)
                if current is None:
                    # Normally this is a message typed mid-turn, but it also happens
                    # at the start of an exchange: when your prompt makes Claude Code
                    # compact the context first, the prompt is queued behind the
                    # compaction and only this attachment carries it. Dropping it left
                    # the panel showing “没找到这一轮的提问” for the whole compaction.
                    current = {'prompt': '', 'start': ts}
                    last_prompt = max(last_prompt, ts)
                keep_first_prompt(current, value)
            continue
        if typ not in ('user', 'assistant'):
            continue
        message = row.get('message') if isinstance(row.get('message'), dict) else {}
        content = message.get('content')
        last_row = max(last_row, ts)
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if typ == 'assistant' and part.get('type') == 'tool_use' and part.get('id'):
                detail = question_detail(part.get('name'), part.get('input'))
                if detail:
                    questions[part['id']] = (detail, ts)
            elif typ == 'user' and part.get('type') == 'tool_result':
                questions.pop(part.get('tool_use_id'), None)      # you answered
        if typ == 'user':
            if isinstance(content, list) and any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content):
                continue                    # tool output, not a person speaking
            text = text_content(content)
            if text.lstrip().startswith(INTERRUPT_MARKERS):
                interrupt = max(interrupt, ts)
                user_interrupted = True
                if current is not None:
                    done, current = current, None
                continue
            if any(row.get(flag) for flag in CLAUDE_NOISE_FLAGS):
                continue                    # written by Claude Code itself, not by you
            value = prompt_preview(text)
            if value:
                last_prompt = max(last_prompt, ts)
                if current is None:
                    current = {'prompt': '', 'start': ts}
                keep_first_prompt(current, value)   # later prompts become follow-ups
        else:
            reply = one_line(text_content(content), PROMPT_LIMIT)
            if reply:
                replies = (replies + [reply])[-3:]
            last_reply = max(last_reply, ts)
            # A turn is over whenever the model stopped on its own terms. `tool_use` means it
            # is still working (more calls are coming); every other reason ends the turn,
            # including `refusal`, `max_tokens` and `pause_turn`. Only checking end_turn left
            # refused or truncated turns looking like they were still running.
            reason = message.get('stop_reason')
            if reason and reason != 'tool_use':
                end_turn = max(end_turn, ts)
                if reason in FAILED_REASONS:
                    failure = max(failure, ts)
                if current is not None:
                    done, current = current, None
    shown = current or done or {}
    return {'prompt': shown.get('prompt', ''), 'start': shown.get('start', 0),
            'followups': shown.get('followups', []), 'followup_count': shown.get('followup_count', 0),
            'prompt_at': last_prompt, 'reply': (replies or [''])[-1],
            'replies': replies, 'reply_at': last_reply, 'end': end_turn,
            'interrupt': interrupt, 'failure': failure,
            'user_interrupted': user_interrupted,
            'last': max(last_prompt, last_reply, last_row),
            'question': max(questions.values(), key=lambda q: q[1]) if questions else None}


def claude_registry(home, now=None):
    """Claude Code keeps one <pid>.json per live session; it carries the live status."""
    now = time.time() if now is None else now
    sessions = {}
    for path in glob.glob(os.path.join(home, '.claude', 'sessions', '*.json')):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        pid, sid = rec.get('pid'), rec.get('sessionId')
        if not isinstance(pid, int) or not isinstance(sid, str) or not sid:
            continue
        # pid 可能被复用、也可能跨容器不可见；再看注册表本身是不是刚被写过。
        status_at = stamp(rec.get('statusUpdatedAt')) or stamp(rec.get('updatedAt'))
        fresh = now - status_at < RECORD_FRESH if status_at else False
        alive = pid_alive(pid) or fresh

        sessions[sid] = {'pid': pid, 'cwd': rec.get('cwd') or '',
                         'name': one_line(rec.get('name'), TITLE_LIMIT),
                         'status': str(rec.get('status') or '').lower() if alive else 'gone',
                         'statusAt': stamp(rec.get('statusUpdatedAt')),
                         'startedAt': stamp(rec.get('startedAt')),
                         'kind': rec.get('kind') or 'interactive'}
    return sessions


def pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def claude_title(rows, rec):
    if rec and rec.get('name'):
        return rec['name']
    for row in reversed(rows):
        name = row.get('customTitle') or row.get('agentName') or row.get('title')
        if isinstance(name, str) and name.strip():
            return one_line(name, TITLE_LIMIT)
    if rec and rec.get('cwd'):
        return one_line(os.path.basename(rec['cwd']) or 'Claude 任务', TITLE_LIMIT)
    return 'Claude 任务'


def collect_claude(home, now, window, host):
    registry = claude_registry(home, now)
    files = []
    cutoff = now - window
    for path in glob.glob(os.path.join(home, '.claude', 'projects', '*', '*.jsonl')):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        files.append((st.st_mtime, path))
    files.sort(reverse=True)
    items, seen, status = [], set(), 'ok'
    for mtime, path in files:
        try:
            rows = tail_rows(path)
        except OSError:
            status = 'partial'
            continue
        sid = claude_session_id(path, rows)
        if sid in seen:
            continue
        seen.add(sid)
        tail = claude_tail(rows)
        rec = registry.get(sid)
        last = max(tail['last'], mtime)
        # 判断“是否还在动”只能用转录里的事件时间；mtime 会因任何一次写盘而刷新。
        quiet = now - (tail['last'] or mtime)
        # The user asked after Claude last finished a reply: this turn is in flight.
        pending = tail['prompt_at'] > tail['end']
        state = ''
        if (tail.get('user_interrupted') and tail['interrupt']
                and tail['interrupt'] >= max(tail['end'], tail['prompt_at'])):
            # The explicit user-stop marker wins over a stale "busy" registry
            # row.  A stopped task is already acknowledged and must not return
            # to the panel as a running item.
            continue
        # Once the latest exchange has a terminal transcript row, trust that
        # row over stale registry state or an older unanswered question. The
        # registry can lag behind both an API error and the user's follow-up.
        terminal = bool(tail.get('end') and tail['end'] >= tail['prompt_at'])
        if terminal and tail.get('failure') and tail['failure'] >= tail['end']:
            state, detail, end = 'error', tail['reply'] or '推理被中断（模型拒绝或超出长度）', tail['failure']
        elif terminal:
            state, detail, end = 'success', tail['reply'] or '已完成', tail['end']
        elif tail['question'] and rec and rec['status'] != 'gone':
            # Claude asked you something (AskUserQuestion) and is waiting for the answer;
            # the registry may still say "busy", so the transcript decides.
            state, detail, end = 'question', tail['question'][0], 0
        elif rec and rec['status'] in CLAUDE_WAITING:
            # Claude is blocked on you (permission prompt), even if the file is quiet.
            state, detail, end = 'waiting', '待批准：在服务器上批准或拒绝后才会继续', 0
        elif (pending and rec and rec['status'] == 'idle'
              and (not tail['interrupt'] or tail['interrupt'] >= tail['prompt_at'])
              and (rec.get('statusAt') or 0) >= tail['prompt_at']):
            # The remote worker has gone idle without an end row.  This is the
            # server-side form of pressing Stop; it is already acknowledged.
            continue
        elif rec and rec['status'] in CLAUDE_RUNNING:
            state, detail, end = 'running', '', 0
        elif tail['interrupt'] and tail['interrupt'] >= max(tail['end'], tail['prompt_at']):
            # (A prompt typed after the stop starts a new exchange; that one is judged below.)
            state, detail, end = 'interrupted', '已中断', tail['interrupt']
        elif pending and quiet < STALE:
            state, detail, end = 'running', '', 0
        elif pending and rec and rec['status'] == 'gone':
            # 注册表还在，但进程已经不在了：任务是被打断的，不是还在跑。
            state, detail, end = 'interrupted', '进程已退出，任务没有结束记录', last
        elif pending and rec:
            state, detail, end = 'interrupted', '长时间没有新进展，进程可能已退出', last
        elif pending:
            state, detail, end = 'unknown', '命令已结束，未收到结果，请查看服务器上的任务', last
        elif tail['end']:
            # A turn that ended on refusal / truncation is an error, not a completion: the work
            # did not happen and you need to see that in the panel.
            if tail.get('failure') and tail['failure'] >= tail['end']:
                state, detail, end = 'error', tail['reply'] or '推理被中断（模型拒绝或超出长度）', tail['failure']
            else:
                state, detail, end = 'success', tail['reply'] or '已完成', tail['end']
        elif rec and rec['status'] == 'idle':
            state, detail, end = 'success', tail['reply'] or '已完成', max(tail['last'], rec.get('statusAt') or 0)
        elif not tail['prompt']:
            continue            # nothing identifiable in this tail
        else:
            state, detail, end = 'unknown', '没有找到结束记录，请查看服务器上的任务', last
        if end and now - end > window:
            continue
        items.append({'agent': 'claude', 'host': host, 'session': sid, 'title': claude_title(rows, rec),
                      'prompt': tail['prompt'], 'state': state, 'detail': one_line(detail, PROMPT_LIMIT),
                      # When this exchange began, not when the Claude process started
                      # (one VS Code session can stay open for hours).
                      'start': tail['start'] or (rec or {}).get('statusAt') or last,
                      'time': end or last, 'resumed': False})
        add_followups(items[-1], tail['followups'], tail['followup_count'])
    # A session that is live but has not written a transcript yet still counts.
    for sid, rec in registry.items():
        if sid in seen:
            continue
        if rec['status'] in CLAUDE_WAITING:
            state = 'waiting'
        elif rec['status'] in CLAUDE_RUNNING:
            state = 'running'
        else:
            continue            # idle or exited, and nothing in the window: no task to show
        items.append({'agent': 'claude', 'host': host, 'session': sid,
                      'title': rec.get('name') or one_line(os.path.basename(rec.get('cwd') or '') or 'Claude 任务', TITLE_LIMIT),
                      'prompt': '', 'state': state, 'detail': '',
                      # statusAt is when it turned busy; startedAt is when the process opened.
                      'start': rec.get('statusAt') or rec.get('startedAt') or now,
                      'time': rec.get('statusAt') or now, 'resumed': False})
    return {'status': status, 'live': len(registry), 'items': items}


# ---------------------------------------------------------------- output

def collect(host, now=None, window=WINDOW, home=None, max_items=40):
    now = time.time() if now is None else now
    home = os.path.expanduser(home or '~')
    out = {'host': host, 'now': now, 'machine': '', 'agents': {}, 'items': [], 'errors': []}
    try:
        out['machine'] = os.uname().nodename
    except AttributeError:
        pass
    for name, fn in (('chatgpt', collect_codex), ('claude', collect_claude)):
        try:
            result = fn(home, now, window, host)
        except Exception as exc:        # one agent must not hide the other
            out['agents'][name] = {'status': 'partial', 'hint': '远端记录暂时无法读取（%s）' % type(exc).__name__}
            out['errors'].append('%s: %s' % (name, type(exc).__name__))
            continue
        out['agents'][name] = {'status': result['status'],
                               'hint': '部分记录暂时无法读取' if result['status'] == 'partial' else ''}
        if name == 'claude':
            out['agents'][name]['live'] = result['live']
        out['items'].extend(result['items'])
    out['items'].sort(key=lambda i: -(i.get('time') or i.get('start') or 0))
    out['items'] = out['items'][:max_items]
    return out


def main(argv):
    host = argv[1] if len(argv) > 1 else 'remote'
    window = float(argv[2]) if len(argv) > 2 else WINDOW
    max_items = int(argv[3]) if len(argv) > 3 else 40
    data = collect(host, window=window, max_items=max_items)
    sys.stdout.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

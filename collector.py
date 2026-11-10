#!/usr/bin/env python3
"""二踢脚大王（AI 桌宠）：只读三个应用的本地会话记录，并可经 SSH 管理远程 Codex / Claude Code。

ChatGPT（本机 com.openai.codex）：rollout JSONL
Claude / Claude-3p 的本地任务：session metadata + audit JSONL
DeepSeek Harness：session.v4.jsonl.zstd 中的 turn/start、turn/end
远程服务器：由 ssh_remote.py 在远端只读汇总，本机只收一行 JSON（见 RemoteHost）
"""
import argparse
import glob
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
try:
    import zstandard
except ImportError:
    zstandard = None

WINDOW = 24 * 3600
STALE = 30 * 60
RESULT_GRACE = 60   # seconds to wait for a result row after a command is marked finished
LOOP_SECONDS = 3
AGENT_LABELS = {'chatgpt': 'ChatGPT', 'claude': 'Claude', 'deepseek': 'DeepSeek Harness'}
LOGGER = logging.getLogger('ai-pet.collector')
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOG_LIMIT = 512 * 1024
_last_logs = {}


def configure_logging(state_dir):
    for handler in LOGGER.handlers[:]:
        handler.close()
        LOGGER.removeHandler(handler)
    path = Path(state_dir)/'collector.log'
    handler = RotatingFileHandler(path, maxBytes=LOG_LIMIT, backupCount=1, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    # Bound a log inherited from an older version before the first new error.
    if path.stat().st_size >= LOG_LIMIT:
        handler.doRollover()
    backup = Path(str(path)+'.1')
    if backup.exists() and backup.stat().st_size > LOG_LIMIT:
        with backup.open('rb') as f:
            f.seek(-LOG_LIMIT, os.SEEK_END)
            tail = f.read()
        backup.write_bytes(tail)
    for file in (path, backup):
        if file.exists():
            file.chmod(0o600)
    LOGGER.addHandler(handler)


def log(message):
    # One repeated error per five minutes, even when a bad file is polled every 3s.
    now = time.monotonic()
    if now - _last_logs.get(message, -float('inf')) < 300:
        return
    if len(_last_logs) >= 64:
        _last_logs.clear()
    _last_logs[message] = now
    LOGGER.warning(message)


def stamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return 0
    return 0


def one_line(text, limit=70):
    text = str(text or '')
    # API keys may occur in prompts. Never copy them into the pet's state file.
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '[密钥已隐藏]', text)
    text = re.sub(r'(?m)^\s*(#+|>+|[-*]\s)\s*', '', text)
    text = re.sub(r'\*\*|__|`', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit-1] + '…'


def first_line(text, limit=50):
    text = str(text or '').strip()
    if text.startswith('/goal'):
        text = text[5:].strip()
    return one_line(text.split('\n', 1)[0], limit)


def json_lines(stream):
    for line in stream:
        # Writers may be halfway through their final record. Retry it next scan.
        if not line.endswith('\n'):
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row
        except (ValueError, UnicodeError):
            continue


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(str(c.get('text', '')) for c in content if isinstance(c, dict) and c.get('type') in ('text', 'input_text'))
    return ''


def new_turn(tid, start=0):
    return {'id': str(tid), 'start': start, 'end': 0, 'state': 'running', 'detail': '', 'prompt': '', 'resumed': False}


def error_text(error):
    """Error payloads are often {'message': ...}; show the message, not the dict."""
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or ('未知错误' if error else ''))
    return str(error or '')


# Blocks the apps inject into a user message; they are not what the user typed.
WRAPPER_BLOCK = re.compile(r'<(system-reminder|environment_context|INSTRUCTIONS|command-name|command-message|'
                           r'command-args|local-command-stdout|local-command-caveat|user-prompt-submit-hook|turn_aborted|'
                           r'task-notification|codex_apps_client_time_context)\b[^>]*>.*?</\1>', re.S)
LONG_PATH = re.compile(r'(?<![\w.])(?:~|/)(?:[^\s/]+/){2,}([^\s/]+/?)')


def prompt_preview(text):
    text = WRAPPER_BLOCK.sub(' ', str(text or '')).strip()
    # Runtime context is not a user's request, even when stored as a user message.
    if text.startswith(('<environment_context>', '<INSTRUCTIONS>', '# AGENTS.md', '<turn_aborted>')):
        return ''
    if text.startswith(INTERRUPT_MARKERS):
        return ''
    if text.startswith('/goal'):
        text = text[5:].strip()
    text = re.sub(r'\\([*_`#>\[\]()~-])', r'\1', text)        # escaped markdown from pasted text
    text = LONG_PATH.sub(r'…/\1', text)                      # keep the file name, drop the long folder path
    return one_line(text, 120)


# Tools that stop the agent until you answer in the app (they are not approvals):
# Claude's AskUserQuestion / ExitPlanMode and DeepSeek Harness's ask_user_question /
# exit_plan_mode. While one has no result yet, the task is waiting on you.
QUESTION_TOOLS = {'askuserquestion': '等你回答', 'ask_user_question': '等你回答',
                  'exitplanmode': '等你确认计划', 'exit_plan_mode': '等你确认计划',
                  # Codex: request_user_input blocks the turn until you answer; the _async one
                  # returns at once and the question stays open until your reply arrives.
                  'request_user_input': '等你回答', 'request_user_input_async': '等你回答'}


# Claude desktop (Cowork) tools that open a permission dialog and wait until you click:
# asking to access a folder, asking to delete files. Shown as 待批准 until the result comes back.
# The name may carry an MCP prefix (mcp__cowork__request_cowork_directory), so match the tail.
APPROVAL_TOOLS = {'request_cowork_directory': ('访问文件夹', 'path'),
                  'allow_cowork_file_delete': ('删除文件', 'file_path')}


def approval_ask(name, args, ts):
    """(label, ts) for a tool that waits on a permission dialog, else None (same shape as DSH approvals)."""
    short = str(name or '').rsplit('__', 1)[-1].lower()
    if short not in APPROVAL_TOOLS:
        return None
    label, field = APPROVAL_TOOLS[short]
    where = args.get(field) if isinstance(args, dict) else ''
    where = LONG_PATH.sub(r'…/\1', str(where or '').strip())
    return (label + (' ' + one_line(where, 40) if where else ''), ts)


def question_ask(name, args, ts):
    """('question', detail, ts) for a question tool call, else None."""
    lead = QUESTION_TOOLS.get(str(name or '').lower())
    if not lead:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    qs = args.get('questions') if isinstance(args, dict) else None
    qs = [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []
    first = one_line(qs[0].get('question') or qs[0].get('title') or qs[0].get('header'), 60) if qs else ''
    count = '%d 个问题' % len(qs) if len(qs) > 1 else ''
    return ('question', lead + (count and ' ' + count) + ('：' + first if first else ''), ts)


QUESTION_REPLY = re.compile(r'<send_user_message_question_reply>(.*?)</send_user_message_question_reply>', re.S)


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


# Codex 的 goal 模式：你设的目标不是普通 UserMessage，而是包在这个标签里的
# response_item。source="user_goal" 是你设目标那一次，source="goal" 是之后自动续跑。
# 和 ssh_remote.py 里的同名定义保持一致：两处解析同一批文件。
GOAL_CONTEXT = re.compile(r'<codex_internal_context[^>]*source="(?:user_goal|goal)"[^>]*>(.*?)</codex_internal_context>', re.S)
GOAL_OBJECTIVE = re.compile(r'<objective>(.*?)</objective>', re.S)
GOAL_SET = re.compile(r'User set the goal:\s*"(.+?)"\s*(?:\n|$)', re.S)


def goal_prompt(text):
    """The goal text from a codex_internal_context block, or '' if this is not one."""
    found = GOAL_CONTEXT.search(str(text or ''))
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


def codex_question_rows(row, turns, latest):
    """Codex records its question tools as response items: open on the call, closed by the
    output with the same call_id (blocking), or by your next message (async)."""
    if row.get('type') != 'response_item':
        return
    p = row.get('payload') or {}
    kind, call = p.get('type'), p.get('call_id')
    if not call:
        return
    if kind in ('function_call', 'custom_tool_call'):
        name = str(p.get('name') or '')
        ask = question_ask(name, p.get('arguments') if kind == 'function_call' else p.get('input'), stamp(row.get('timestamp')))
        meta = p.get('internal_chat_message_metadata_passthrough') or {}
        t = turns.get(meta.get('turn_id')) or latest
        if ask and t is not None:
            prefix = 'qa:' if name.endswith('_async') else 'q:'
            t.setdefault('asks', {})[prefix + str(call)] = ask
    elif kind in ('function_call_output', 'custom_tool_call_output'):
        for t in turns.values():
            t.get('asks', {}).pop('q:' + str(call), None)      # the async one is answered later


def clear_async_questions(turns):
    """You wrote in the thread again (an answer or anything else): open async questions are done."""
    for t in turns.values():
        for key in [k for k in t.get('asks', {}) if k.startswith('qa:')]:
            t['asks'].pop(key)


def open_async_question(turn):
    asks = [a for k, a in (turn.get('asks') or {}).items() if k.startswith('qa:')]
    return max(asks, key=lambda a: a[-1]) if asks else None


def prompt_time(turn):
    """When you asked this turn yourself; 0 for a turn that started without your message
    (Codex resuming by itself, a DeepSeek turn with no user message)."""
    if not turn.get('prompt') or turn.get('prompt_inherited'):
        return 0
    return turn.get('asked_at') or turn['start']


# 已结束但不算“看过”的状态：出错和中断要留着，否则真正的问题会被静默吞掉。
KEPT_AFTER_NEW_PROMPT = ('error', 'interrupted', 'blocked', 'unknown')


def seen_by_new_prompt(turn, prompt_times):
    """You sent a new prompt in this conversation after this turn had finished: you have
    seen its result, so it leaves the panel, as on the servers (one task per session there).

    Only successful turns are retired this way. An error, a stop or an unclear outcome
    stays until you look at it: the next prompt is not proof that you read the failure.
    """
    if turn.get('state') in KEPT_AFTER_NEW_PROMPT:
        return False
    finished = turn['end'] or turn.get('soft_end') or 0
    return bool(finished) and any(a > finished for a in prompt_times)


FOLLOWUPS_KEPT = 2      # the panel shows the latest two follow-ups and a count of the rest


def keep_first_prompt(turn, value):
    """A turn is remembered by what the user first asked. Messages you send while it is
    still working are kept as follow-ups (追加), not as a new task. A prompt inherited by
    an auto-resumed turn gives way to the user's own first message."""
    if not value:
        return
    if not turn.get('prompt') or turn.get('prompt_inherited'):
        turn['prompt'] = value
        turn['prompt_inherited'] = False
        return
    seen = turn.setdefault('seen_prompts', set())
    if value == turn['prompt'] or value in seen:
        return              # the same message logged twice (Codex writes two events per message)
    seen.add(value)
    turn['followups'] = (turn.get('followups', []) + [value])[-FOLLOWUPS_KEPT:]
    turn['followup_count'] = turn.get('followup_count', 0) + 1


def remote_followups(item):
    """Follow-ups reported by a server probe: strings only, previewed, capped."""
    raw = item.get('followups')
    values = [prompt_preview(v) for v in raw if isinstance(v, str)] if isinstance(raw, list) else []
    values = [v for v in values if v][-FOLLOWUPS_KEPT:]
    if not values:
        return {}
    try:
        count = int(item.get('followupCount') or 0)
    except (TypeError, ValueError):
        count = 0
    return {'followups': values, 'followupCount': max(len(values), min(count, 999))}


def parse_codex(path):
    """Parse one Codex rollout file (local side, ChatGPT desktop app).

    Deliberately duplicated in ssh_remote.py (server side, codex CLI). The two apps
    share a file format but are not the same: originator is codex_work_desktop vs
    codex-tui, the desktop channel ships alpha builds and has goal mode. Keep the
    two in step by hand — a fix here usually belongs there too.
    """
    info = {'id': '', 'title': '', 'skip': False, 'turns': {}}
    goal = prompt = ''
    latest = previous = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for row in json_lines(f):
            p = row.get('payload') or {}
            if not isinstance(p, dict):
                continue
            ts = stamp(row.get('timestamp'))
            if row.get('type') == 'session_meta' and not info['id']:
                info['id'] = p.get('id') or p.get('session_id') or ''
                source = json.dumps(p.get('thread_source') or p.get('source') or '').lower()
                info['skip'] = bool(p.get('parent_thread_id')) or any(s in source for s in ('subagent', 'sub_agent', 'spawn'))
            codex_question_rows(row, info['turns'], latest)
            if row.get('type') == 'response_item':
                # goal 模式下，你设的目标不会写成 UserMessage，只在这里出现。
                if p.get('type') == 'message' and p.get('role') == 'user':
                    value = goal_prompt(text_content(p.get('content')))
                    if value and latest is not None:
                        keep_first_prompt(latest, value)
                continue
            if row.get('type') != 'event_msg':
                continue
            ev, tid = p.get('type'), p.get('turn_id')
            if ev in ('task_started', 'task_complete') and tid:
                previous = latest
                latest = info['turns'].setdefault(tid, new_turn(tid))
                latest['start'] = latest['start'] or stamp(p.get('started_at')) or ts
                if ev == 'task_started' and (p.get('turn_attribution') or {}).get('turn_trigger') == 'resume_interrupted_task':
                    # Codex restarts an interrupted turn by itself; it carries no new prompt.
                    latest['resumed'] = True
                    if not latest['prompt'] and previous is not None and previous is not latest and previous['prompt']:
                        latest['prompt'] = previous['prompt']
                        latest['prompt_inherited'] = True
                if ev == 'task_complete':
                    latest['end'] = stamp(p.get('completed_at')) or ts
                    latest['state'] = 'error' if p.get('error') else 'success'
                    latest['detail'] = one_line(error_text(p.get('error')) or p.get('last_agent_message') or '已完成')
            elif ev in ('turn_aborted', 'task_aborted'):
                t = info['turns'].get(tid) if tid else latest
                if t:
                    t.update(end=ts, state='interrupted', detail='已中断')
            elif ev == 'item_completed':
                item = p.get('item') or {}
                if item.get('type') == 'UserMessage':
                    raw = text_content(item.get('content'))
                    value = question_reply(raw) or prompt_preview(raw)
                    if value:
                        clear_async_questions(info['turns'])
                    prompt = prompt or value
                    t = info['turns'].get(tid) if tid else latest
                    if t:
                        keep_first_prompt(t, value)
            elif ev == 'user_message':
                value = question_reply(p.get('message')) or prompt_preview(p.get('message'))
                if value:
                    clear_async_questions(info['turns'])
                prompt = prompt or value
                t = info['turns'].get(tid) if tid else latest
                if t:
                    keep_first_prompt(t, value)
            elif ev == 'thread_goal_updated' and not goal:
                goal = (p.get('goal') or {}).get('objective') or ''
            elif ev == 'thread_name_updated':
                info['title'] = p.get('thread_name') or info['title']
    info['title'] = one_line(info['title'], 50) or first_line(goal or prompt) or 'ChatGPT 当前任务'
    return info


INTERRUPT_MARKERS = ('[Request interrupted by user', '[Request cancelled by user', '[Request aborted')


# Messages Claude writes into the conversation by itself, not something you typed:
# the summary after the context is compacted, skill / command expansions, command output.
CLAUDE_NOISE_PREFIXES = ('<', 'This session is being continued', 'Caveat:', '# CLAUDE.md', '# AGENTS.md',
                         'Base directory for this skill', 'Continue from where you left off')
CLAUDE_NOISE_FLAGS = ('isMeta', 'isCompactSummary', 'isVisibleInTranscriptOnly', 'isSynthetic')


def claude_prompt(row):
    """What you typed in this user row, or '' when Claude itself wrote it."""
    if any(row.get(flag) for flag in CLAUDE_NOISE_FLAGS):
        return ''
    raw = WRAPPER_BLOCK.sub(' ', text_content((row.get('message') or {}).get('content'))).strip()
    if raw.startswith(CLAUDE_NOISE_PREFIXES):
        return ''
    return prompt_preview(raw)


def interrupt_marker(row):
    text = text_content((row.get('message') or {}).get('content'))
    return any(text.lstrip().startswith(m) for m in INTERRUPT_MARKERS)


def queued_prompt(row):
    """Text of a message you typed while Claude was working (a queued_command attachment).

    Only real prompts: background-task notices use the same attachment with another
    commandMode and must not show up as something you asked.
    """
    att = row.get('attachment') if isinstance(row.get('attachment'), dict) else {}
    if att.get('type') != 'queued_command' or att.get('commandMode') not in (None, '', 'prompt'):
        return ''
    raw = WRAPPER_BLOCK.sub(' ', text_content(att.get('prompt'))).strip()
    return '' if raw.startswith(CLAUDE_NOISE_PREFIXES) else prompt_preview(raw)


def fold_claude_followups(turns, created):
    """A message you send while Claude is still working is answered in the same run.

    The app gives it its own command, but that command usually never gets a
    result row of its own, so on its own it would end up as 待确认. Fold every
    command created while an earlier one was still running into that earlier one
    (one task, your first prompt), unless it later ran by itself and produced its
    own outcome after the earlier one had finished.
    """
    order = sorted(turns.values(), key=lambda t: (created.get(t['id']) or t['start'] or 0, t['id']))
    lead = None
    for t in order:
        if lead is not None:
            # closed_at: a merged follow-up began executing, so the earlier run was over
            # by then even if it never logged an end (one command runs at a time).
            finish = lead['end'] or lead.get('soft_end') or lead.get('closed_at') or (
                float('inf') if lead.get('started_at') else lead['start'])
            made = created.get(t['id']) or t['start'] or 0
            # Ran by itself: it started after the earlier one truly ended (a result,
            # stop or error, not just "command completed"). After a success it also
            # needs its own outcome; after a stop or error it is new work either way.
            ran_alone = bool(lead['end']) and t.get('started_at', 0) > lead['end'] and (
                lead['state'] != 'success' or t['end'] or not t.get('soft_end'))
            if made <= finish and not ran_alone:
                if not lead['end'] and t['end']:
                    # The earlier command finished without a result; the follow-up carries it.
                    lead.update(end=t['end'], state=t['state'], detail=t['detail'])
                    lead.pop('soft_end', None)
                elif not lead['end'] and t.get('soft_end'):
                    lead['soft_end'] = max(lead.get('soft_end') or 0, t['soft_end'])
                if not lead['end'] and not lead.get('soft_end') and t.get('started_at'):
                    lead['closed_at'] = min(lead.get('closed_at') or t['started_at'], t['started_at'])
                # Your later message becomes a follow-up (追加) of this task, in order.
                for value in [t.get('prompt', '')] + t.get('followups', []):
                    keep_first_prompt(lead, value)
                if t.get('asks'):
                    lead.setdefault('asks', {}).update(t['asks'])   # a question asked in the merged run
                del turns[t['id']]
                continue
        if t.get('started_at') or t['end']:
            lead = t


def parse_claude(path):
    info = {'id': path.parent.name, 'title': '', 'skip': False, 'turns': {}, 'tail': []}
    active = None
    prompts = {}
    created = {}            # command id -> when you sent it
    with open(path, encoding='utf-8', errors='replace') as f:
        for row in json_lines(f):
            if row.get('parent_tool_use_id'):
                continue
            typ, ts = row.get('type'), stamp(row.get('timestamp') or row.get('_audit_timestamp'))
            if typ == 'user':
                value = claude_prompt(row)
                tid = row.get('uuid')
                if value and tid:
                    prompts[tid] = value
                    created[tid] = min(created.get(tid) or ts, ts) if ts else created.get(tid, 0)
                    if tid in info['turns']:
                        info['turns'][tid]['prompt'] = value
            if typ:
                # Structure only (no content), for diagnosing turns that never end.
                info['tail'] = (info['tail'] + [str(typ)[:24] + (':' + str(row.get('state') or row.get('subtype') or '')[:16] if (row.get('state') or row.get('subtype')) else '')])[-8:]
            if typ == 'attachment' and active is not None and not active['end']:
                # Claude Code stores a message typed mid-turn as a queued_command attachment.
                value = queued_prompt(row)
                if value:
                    keep_first_prompt(active, value)
            if typ in ('assistant', 'user'):
                # AskUserQuestion / ExitPlanMode wait for your answer: open from the tool_use
                # until its tool_result comes back.
                content = (row.get('message') or {}).get('content')
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if typ == 'assistant' and part.get('type') == 'tool_use' and part.get('id'):
                        # A question (待回答) or a permission dialog such as folder access (待批准).
                        ask = question_ask(part.get('name'), part.get('input'), ts) \
                            or approval_ask(part.get('name'), part.get('input'), ts)
                        owner = active or (max(info['turns'].values(), key=lambda x: x['start']) if info['turns'] else None)
                        if ask and owner is not None:
                            owner.setdefault('asks', {})['q:' + str(part['id'])] = ask
                    elif typ == 'user' and part.get('type') == 'tool_result' and part.get('tool_use_id'):
                        for t in info['turns'].values():
                            t.get('asks', {}).pop('q:' + str(part['tool_use_id']), None)
            if typ == 'user' and active is not None and not active['end'] and interrupt_marker(row):
                # The SDK records a stop as a synthetic user message, often with no result row.
                active.update(end=ts, state='interrupted', detail='已中断')
            if typ == 'command_lifecycle' and row.get('command_uuid'):
                tid = row['command_uuid']
                t = info['turns'].setdefault(tid, new_turn(tid, ts))
                created[tid] = min(created.get(tid) or ts, ts) if ts else created.get(tid, 0)
                t['prompt'] = prompts.get(tid, t['prompt'])
                state = str(row.get('state') or '').lower()
                if state in ('started', 'running'):
                    # Only a command that is actually executing can receive a stop marker;
                    # a queued one behind it must not be ended by the earlier command's stop.
                    active = t
                    t.setdefault('started_at', ts)
                    t['queued'] = False
                    t.pop('soft_end', None)
                    if t['state'] == 'interrupted':
                        t.update(end=0, state='running', detail='')
                    if state == 'started':
                        t['start'] = ts
                elif state in ('queued', 'pending'):
                    t['queued'] = True
                elif any(k in state for k in ('fail', 'error')):
                    t.update(end=ts, state='error', detail='任务出错')
                elif any(k in state for k in ('cancel', 'abort', 'interrupt', 'stop', 'kill')):
                    t.update(end=ts, state='interrupted', detail='已中断')
                elif state in ('completed', 'complete', 'finished', 'done', 'succeeded', 'success') and not t['end']:
                    # Success still requires a result row. If none follows within
                    # RESULT_GRACE, its outcome is unknown. Other states (e.g. waiting
                    # for permission) leave the turn running.
                    t['soft_end'] = ts
            elif typ == 'result':
                tid = row.get('user_message_uuid') or (active or {}).get('id') or row.get('uuid')
                if not tid:
                    continue
                t = info['turns'].setdefault(tid, new_turn(tid, ts))
                created[tid] = min(created.get(tid) or ts, ts) if ts else created.get(tid, 0)
                t['queued'] = False
                t['prompt'] = prompts.get(tid, t['prompt'])
                reason = row.get('terminal_reason')
                state = 'error' if row.get('is_error') else 'interrupted' if reason in ('aborted', 'interrupted', 'cancelled') else 'success' if row.get('subtype') == 'success' else 'unknown'
                t.update(end=ts, state=state, detail=one_line(row.get('result')) or {'success':'已完成', 'error':'任务出错', 'interrupted':'已中断', 'unknown':'回合结束，结果待确认'}[state])
                active = t
    fold_claude_followups(info['turns'], created)
    for t in info['turns'].values():
        # When you sent it, not when it began running (a queued message waits for its turn).
        t['asked_at'] = created.get(t['id']) or t['start']
    return info


def parse_deepseek(path):
    if zstandard is None:
        raise RuntimeError('缺少随应用打包的 zstandard 解码组件')
    info = {'id': path.parent.name, 'title': '', 'skip': False, 'turns': {}}
    latest = None
    with open(path, 'rb') as f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
        with io.TextIOWrapper(reader, encoding='utf-8', errors='replace') as text:
            for row in json_lines(text):
                typ, data = row.get('type'), row.get('data') or {}
                if not isinstance(data, dict):
                    continue
                ts = stamp(row.get('time'))
                if typ == 'session':
                    info['id'] = row.get('id') or info['id']
                    info['skip'] = bool(row.get('parentSession') or row.get('delegationDepth', 0))
                elif typ == 'session/title':
                    info['title'] = one_line(data.get('title'), 50)
                elif typ == 'turn/start':
                    tid = str(data.get('turn', row.get('seq')))
                    latest = info['turns'].setdefault(tid, new_turn(tid, ts))
                elif typ == 'approval/asked' and latest is not None and data.get('id'):
                    # Harness stopped to ask you before running a tool; it resumes after approval/decided.
                    latest.setdefault('asks', {})[str(data['id'])] = (one_line(data.get('toolName'), 40), ts)
                elif typ == 'approval/decided' and latest is not None:
                    latest.get('asks', {}).pop(str(data.get('id')), None)
                elif typ == 'tool/call' and data.get('callId'):
                    # ask_user_question / exit_plan_mode: waits for your answer, no approval event.
                    ask = question_ask(data.get('name'), data.get('arguments'), ts)
                    t = info['turns'].get(str(data.get('turn'))) or latest
                    if ask and t is not None:
                        t.setdefault('asks', {})['q:' + str(data['callId'])] = ask
                elif typ == 'tool/result':
                    msg = data.get('message') if isinstance(data.get('message'), dict) else {}
                    call = msg.get('toolCallId') or (msg.get('source') or {}).get('callId')
                    if call:
                        for t in info['turns'].values():
                            t.get('asks', {}).pop('q:' + str(call), None)
                elif typ == 'assistant/message':
                    # The reply text. Most steps carry only reasoning or tool calls; keep the
                    # latest step that has visible text, so the panel can show the answer.
                    # Only trust the turn id: falling back to `latest` would attach a reply
                    # to whichever turn happened to start last.
                    t = info['turns'].get(str(data.get('turn'))) if data.get('turn') is not None else None
                    msg = data.get('message') if isinstance(data.get('message'), dict) else {}
                    reply = one_line(text_content(msg.get('content')), 120)
                    if t is not None and reply:
                        t['reply'] = reply
                elif typ == 'user/message' and (data.get('source') or {}).get('kind', 'user') == 'user':
                    value = prompt_preview(text_content(data.get('content')))
                    if latest:
                        keep_first_prompt(latest, value)
                elif typ == 'turn/end':
                    tid = str(data.get('turn', (latest or {}).get('id', row.get('seq'))))
                    t = info['turns'].setdefault(tid, new_turn(tid, ts))
                    why = data.get('reason') if isinstance(data.get('reason'), dict) else {}
                    cause = why.get('reason') if isinstance(why.get('reason'), dict) else {}
                    reason = why.get('kind')
                    # 'blocked': a plugin hook (agent/pre-step) refused the next step, e.g. the
                    # session was archived; the turn just ends. Asking for permission is
                    # approval/asked (待批准), not this, so it counts as finished.
                    state = {'completed':'success', 'error':'error', 'aborted':'interrupted', 'interrupted':'interrupted', 'blocked':'success', 'max-tokens':'error'}.get(reason, 'unknown')
                    detail = {'success':'已完成', 'error':'运行出错', 'interrupted':'已中断', 'unknown':'回合结束，结果待确认'}[state]
                    if state == 'success' and t.get('reply') and reason != 'blocked':
                        detail = t['reply']
                    if reason == 'blocked':
                        detail = '下一步被插件规则拦下（如会话已归档），这一轮已结束'
                    elif reason == 'max-tokens':
                        detail = '达到长度上限'
                    elif reason == 'error' and error_text(why.get('error')):
                        detail = '出错：' + error_text(why.get('error'))
                    elif reason == 'aborted' and cause.get('kind') == 'disposed':
                        detail = '应用关闭时中断'
                    t.update(end=ts, state=state, detail=one_line(detail))
    info['title'] = info['title'] or 'DeepSeek 对话'
    return info


class Reader:
    def __init__(self, agent, root, pattern, parser):
        self.agent, self.root, self.pattern, self.parser = agent, Path(root), pattern, parser
        self.cache = {}
        self.stale_logged = {}
        self.title_cache = {}
        self.title_key = None

    def conversation_titles(self):
        """The app's display names live separately from its conversation logs.

        Reload this index independently, so a rename updates even an idle task.
        Partial records and a temporarily unavailable index keep prior names.
        """
        if self.agent != 'chatgpt':
            return {}
        path = self.root/'session_index.jsonl'
        try:
            st = path.stat()
            key = (st.st_ino, st.st_size, st.st_mtime_ns)
            if key != self.title_key:
                titles = dict(self.title_cache)
                with path.open(encoding='utf-8', errors='replace') as f:
                    for row in json_lines(f):
                        name = one_line(row.get('thread_name'), 120)
                        if row.get('id') and name:
                            titles[row['id']] = name
                self.title_cache, self.title_key = titles, key
        except OSError:
            pass
        return self.title_cache

    def files(self):
        if not self.root.is_dir():
            return []
        return list(self.root.glob(self.pattern))

    def collect(self, now, alive):
        running, done, pending = [], [], []
        errors = 0
        files = self.files()
        if not self.root.is_dir():
            return running, done, pending, 'missing'
        titles = self.conversation_titles()
        seen = set()
        for path in files:
            try:
                st = path.stat()
                # Old sessions remain eligible when they receive a new write.
                if now - st.st_mtime > WINDOW:
                    continue
                seen.add(path)
                key = (st.st_ino, st.st_size, st.st_mtime_ns)
                cached = self.cache.get(path)
                if cached and cached[0] == key:
                    info = cached[1]
                else:
                    info = self.parser(path)
                    self.cache[path] = (key, info)
                if info.get('skip') or not info.get('id') or not info['turns']:
                    continue
                if info['id'] in titles:
                    info = dict(info, title=titles[info['id']])
                if self.agent == 'claude':
                    # audit folder is the first eight characters of the local session UUID.
                    meta = next(path.parent.parent.glob('local_'+path.parent.name+'*.json'), None)
                    if meta:
                        with open(meta, encoding='utf-8') as f:
                            m = json.load(f)
                        if m.get('isArchived'):
                            continue
                        info = dict(info, id=m.get('sessionId') or info['id'], title=one_line(m.get('title'), 50) or 'Claude 任务')
                started_turns = [t for t in info['turns'].values() if not t.get('queued')]
                latest_turn = max(started_turns or list(info['turns'].values()), key=lambda t: t['start'])
                prompt_times = [prompt_time(x) for x in info['turns'].values()]
                for t in info['turns'].values():
                    # A resumed conversation supersedes prior interrupted/unknown turns.
                    if t is not latest_turn and not t.get('queued') and (not t['end'] or t['state'] in ('interrupted', 'unknown')):
                        continue
                    if seen_by_new_prompt(t, prompt_times):
                        continue
                    target = {'type': 'url', 'value': 'codex://threads/'+info['id']} if self.agent == 'chatgpt' else {'type': 'app', 'value': {'claude':'com.anthropic.claudefordesktop', 'deepseek':'com.deepseek.dsh'}[self.agent]}
                    tid = 'codex:'+t['id'] if self.agent == 'chatgpt' else self.agent+':'+info['id']+':'+t['id']
                    if not t['end'] and t.get('soft_end') and now - t['soft_end'] > RESULT_GRACE:
                        # Finishing a command does not prove the task succeeded or was stopped.
                        t = dict(t, end=t['soft_end'], state='unknown', detail='命令已结束，未收到结果，请查看应用')
                    item = dict(id=tid, agent=self.agent, title=info['title'] or '未命名任务', prompt=t.get('prompt', ''), detail=t['detail'], start=t['start'] or st.st_mtime, open=target, state=t['state'], resumed=bool(t.get('resumed')))
                    if t.get('followups'):
                        item.update(followups=list(t['followups']), followupCount=t.get('followup_count', len(t['followups'])))
                    asks = t.get('asks') or {}
                    if not t['end'] and asks and alive:
                        # Waiting for your approval or your answer: no writes happen meanwhile,
                        # so it must not go stale. Approvals are (tool, ts); questions are
                        # ('question', detail, ts).
                        newest = max(asks.values(), key=lambda a: a[-1])
                        if len(newest) == 3:
                            item.update(state='question', detail=newest[1], time=newest[2])
                        else:
                            tool, asked_at = newest
                            item.update(state='waiting', detail='等你批准' + ('：' + tool if tool else ''), time=asked_at)
                        pending.append(item)
                    elif not t['end']:
                        if alive and now-st.st_mtime < STALE:
                            if t.get('queued'):
                                item['detail'] = '已排队，等待开始'
                            if item['resumed'] and not item['detail']:
                                item['detail'] = '上一轮停止或出错后，Codex 自动继续'
                            running.append(item)
                        else:
                            detail = '超过 30 分钟没有新进展，请检查 API 或应用' if alive else '应用未运行，尚未收到结束记录'
                            item.update(state='unknown', detail=detail, time=st.st_mtime)
                            pending.append(item)
                            if info.get('tail') and self.stale_logged.get(path) != key:
                                # Once per file version; record types only, no content.
                                self.stale_logged[path] = key
                                log('%s 回合没有结束记录 %s，最后几条记录类型：%s' % (self.agent, path.parent.name[:8], ' '.join(info['tail'])))
                    elif now-t['end'] <= WINDOW:
                        item['time'] = t['end']
                        open_q = open_async_question(t) if t['state'] == 'success' else None
                        if open_q:
                            # Codex asked with request_user_input_async and the turn ended
                            # before you answered: it is waiting on you, not finished.
                            item.update(state='question', detail=open_q[1])
                        (done if item['state'] == 'success' else pending).append(item)
            except Exception as exc:
                # A broken or concurrently replaced file must not hide the other agents.
                errors += 1
                if not isinstance(exc, (ValueError, FileNotFoundError)):
                    log('%s 会话暂时无法读取：%s' % (self.agent, type(exc).__name__))
        self.cache = {p:v for p,v in self.cache.items() if p in seen}
        self.stale_logged = {p:v for p,v in self.stale_logged.items() if p in seen}
        return running, done, pending, 'partial' if errors else 'ok'


def make_readers(base=None, config=None):
    base = Path(base or Path.home())
    roots = local_roots(config or default_config(), base)
    support = base/'Library/Application Support'
    claude = roots['claude']
    return [
        Reader('chatgpt', os.environ.get('CODEX_HOME') or roots['codex'], 'sessions/*/*/*/rollout-*.jsonl', parse_codex),
        Reader('claude', claude/'local-agent-mode-sessions', '*/*/*/audit.jsonl', parse_claude),
        Reader('claude', support/'Claude-3p/local-agent-mode-sessions', '*/*/*/audit.jsonl', parse_claude),
        Reader('deepseek', roots['deepseek'], '*/session-*/session.v4.jsonl.zstd', parse_deepseek),
    ]



# ------------------------------------------------------------------ 用户配置
#
# 所有可调项都在应用数据目录的 config.json 里，不再和某台机器绑定：
# 换一台电脑、换成 ssh config 里别的主机名，都不用改代码。
# 文件缺失或损坏时退回默认值，桌宠不会因为配置坏了就打不开。

CONFIG_VERSION = 1
DEFAULT_CONFIG = {
    'version': CONFIG_VERSION,
    'local': {
        # 这几个路径默认取自当前用户的家目录；留空表示用默认位置。
        'codex': '', 'claude': '', 'deepseek': '',
    },
    'remote': {'enabled': False, 'hosts': []},
    'appearance': {'scale': 1.0},
}

# Codex / Claude / DeepSeek 的默认记录位置（相对家目录）。
DEFAULT_LOCAL_PATHS = {
    'codex': '.codex',
    'claude': 'Library/Application Support/Claude',
    'deepseek': '.dsh/sessions',
}


def default_config():
    import copy
    return copy.deepcopy(DEFAULT_CONFIG)


def config_path(state_dir):
    return Path(state_dir)/'config.json'


def _as_bool(value, fallback):
    return value if isinstance(value, bool) else fallback


def _as_scale(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(1.8, max(0.6, number)) if number == number else fallback


def normalise_hosts(entries):
    """同一个校验既管配置文件，也管面板提交上来的服务器列表。"""
    result, seen = [], set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get('enabled') is False:
            continue
        alias = ssh_alias(entry)
        if not alias:
            continue
        host_id = one_line(entry.get('id') or alias, 24)
        if host_id in seen:
            continue            # 同一台机器配两遍没有意义
        seen.add(host_id)
        label = one_line(entry.get('label'), 24) if entry.get('label') else short_label(host_id)
        result.append({'id': host_id, 'alias': alias, 'label': label})
    return result


def normalise_config(raw, fallback=None):
    """Accept anything and return a usable config; unknown fields are dropped."""
    base = fallback or default_config()
    if not isinstance(raw, dict):
        return base
    out = default_config()
    local = raw.get('local') if isinstance(raw.get('local'), dict) else {}
    for key in ('codex', 'claude', 'deepseek'):
        value = local.get(key)
        # A path is either empty (use the default) or an absolute path.
        out['local'][key] = str(value)[:400] if isinstance(value, str) and value.startswith('/') else ''
    remote = raw.get('remote') if isinstance(raw.get('remote'), dict) else {}
    out['remote']['enabled'] = _as_bool(remote.get('enabled'), base['remote']['enabled'])
    out['remote']['hosts'] = normalise_hosts(remote.get('hosts'))
    look = raw.get('appearance') if isinstance(raw.get('appearance'), dict) else {}
    out['appearance']['scale'] = _as_scale(look.get('scale'), base['appearance']['scale'])
    return out


def load_config(state_dir):
    """Read config.json. Missing means a fresh install (defaults); broken means defaults + a log line."""
    path = config_path(state_dir)
    try:
        with open(path, encoding='utf-8') as f:
            return normalise_config(json.load(f))
    except FileNotFoundError:
        return default_config()
    except (OSError, ValueError):
        log('配置文件无法读取，先用默认设置')
        return default_config()


def save_config(state_dir, config):
    """Write config.json atomically with owner-only permissions."""
    config = normalise_config(config)
    write_atomic(config_path(state_dir), json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    return config


def local_roots(config, base=None):
    """Where each agent keeps its records, honouring overrides from config.json."""
    base = Path(base or Path.home())
    roots = {}
    for key, default in DEFAULT_LOCAL_PATHS.items():
        override = (config.get('local') or {}).get(key) or ''
        roots[key] = Path(override) if override else base/default
    return roots


# ------------------------------------------------------------------ 远程服务器
#
# 远端只读：ssh_remote.py 被送到远端后汇总 Codex / Claude Code 的记录，回一行 JSON。
# 这里只负责调用、超时、失败退避，并把远端任务并进同一份状态。每台机器一个线程，
# 采集循环从不等 SSH（一次往返可能要几秒）；连不上时冻结上次结果并标出原因。

SSH_BIN = '/usr/bin/ssh'   # 系统自带，不依赖 Command Line Tools
SSH_BATCH = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=10',
             '-o', 'ServerAliveCountMax=2', '-o', 'LogLevel=ERROR', '-o', 'StrictHostKeyChecking=yes']
REMOTE_WINDOW = WINDOW
REMOTE_MAX_ITEMS = 40
HOST_POLL = 10.0          # 每台机器最多 10 秒拉一次
HOST_BACKOFF = 60.0       # 连续失败时的最长间隔


def short_label(text):
    """Panel and menu names stay short: gpu-box.example -> example, 192.168.0.7 -> 7."""
    text = one_line(text, 24)
    return text.rsplit('.', 1)[-1] if '.' in text else text


SSH_ALIAS = re.compile(r'[A-Za-z0-9_.:@%+](?:[A-Za-z0-9_.:@%+-]{0,199})?')


def ssh_alias(host):
    """An ssh destination string safe to hand to /usr/bin/ssh as one argument.

    The first character must not be '-': ssh would read it as an option
    (for example `-oProxyCommand=...`), which is why a bare character class
    is not enough.
    """
    alias = str(host.get('alias') or host.get('id') or '')
    return alias if SSH_ALIAS.fullmatch(alias) else ''


def remote_script():
    return Path(__file__).resolve().parent/'ssh_remote.py'


class RemoteHost:
    """One SSH server: periodic, bounded, read-only probe in its own thread."""

    def __init__(self, host, script=None, runner=None):
        self.id = host['id']
        self.alias = host['alias']
        self.label = host.get('label') or short_label(self.id)
        self.script = Path(script) if script else remote_script()
        self.runner = runner or self.ssh
        self.items = []
        self.status = 'pending'
        self.hint = ''
        self.machine = ''
        self.live = 0
        self.fetched = 0          # when this sample was produced (local clock)
        self.failures = 0
        self.nextAt = 0.0
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()

    def ssh(self):
        payload = self.script.read_bytes()
        # 参数顺序与 ssh_remote.py 的 main() 一致：<host-id> <window> <max-items>。
        host_id = self.id if re.fullmatch(r'[A-Za-z0-9_.:@%+-]{1,24}', self.id) else 'remote'
        # 脚本走 stdin（不落远端磁盘），参数走 ssh 命令行。
        proc = subprocess.run(
            [SSH_BIN] + SSH_BATCH + [self.alias, 'python3 - %s %d %d' % (host_id, REMOTE_WINDOW, REMOTE_MAX_ITEMS)],
            input=payload, capture_output=True, timeout=45)
        if proc.returncode != 0:
            raise RuntimeError(self.error_hint(proc))
        return proc.stdout

    @staticmethod
    def error_hint(proc):
        """Turn ssh's stderr into one short line. Never echoes the command or env."""
        text = (proc.stderr or b'').decode('utf-8', 'replace')
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith('**')]
        for line in lines:
            if 'Permission denied' in line:
                return 'SSH 认证失败，请检查密钥或服务器是否允许登录'
            if 'Host key verification failed' in line or 'REMOTE HOST IDENTIFICATION' in line:
                return 'SSH 主机密钥校验失败，请先在本机手动 ssh 一次确认'
            if 'No such file' in line or 'not found' in line:
                return '服务器上找不到 python3，无法读取会话记录'
            if 'Connection refused' in line:
                return '服务器拒绝连接（端口未开放）'
        if proc.returncode == 255:
            return 'SSH 连接失败'
        return '服务器返回错误（%s）' % (lines[-1][:80] if lines else 'exit %d' % proc.returncode)

    def poll(self):
        try:
            raw = self.runner()
            data = json.loads(raw.decode('utf-8', 'replace').strip().splitlines()[-1])
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                raise ValueError('bad payload')
        except subprocess.TimeoutExpired:
            return self.record_failure('连接超时，服务器可能繁忙或网络不可达')
        except (OSError, ValueError, RuntimeError, IndexError) as exc:
            return self.record_failure(str(exc) if isinstance(exc, RuntimeError) else '暂时无法读取远端会话')
        with self.lock:
            items = [self.normalise(i) for i in data['items'] if isinstance(i, dict)]
            items = [i for i in items if i['agent']]
            self.items = list({i['id']: i for i in items}.values())   # one row per task
            self.status = 'ok'
            self.hint = ''
            self.machine = one_line(data.get('machine'), 60)
            self.live = int(((data.get('agents') or {}).get('claude') or {}).get('live') or 0)
            self.fetched = time.time()
            self.failures = 0
        return True

    def record_failure(self, reason):
        with self.lock:
            self.failures += 1
            self.status = 'offline' if not self.items else 'stale'
            self.hint = one_line(reason, 80)
        log('远程 %s 读取失败：%s' % (self.id, reason))
        return False

    def normalise(self, item):
        agent = str(item.get('agent') or '')
        if agent not in ('chatgpt', 'claude'):
            return {'agent': ''}
        session = one_line(item.get('session'), 80)
        title = one_line(item.get('title'), 50) or '远程任务'
        state = str(item.get('state') or 'unknown')
        if state not in ('running', 'success', 'error', 'interrupted', 'waiting', 'question', 'unknown'):
            state = 'unknown'
        start = stamp(item.get('start'))
        when = stamp(item.get('time')) or start
        turn = one_line(item.get('turn'), 60)
        cwd = item.get('cwd') if isinstance(item.get('cwd'), str) else ''
        return {
            'id': 'ssh:%s:%s:%s:%s' % (self.id, agent, session, turn or when or start),
            'agent': agent, 'title': title, 'prompt': prompt_preview(item.get('prompt')),
            **remote_followups(item),
            'detail': one_line(item.get('detail'), 90), 'start': start, 'time': when,
            'state': state, 'resumed': bool(item.get('resumed')),
            'host': self.id, 'hostLabel': self.label, 'session': session,
            'open': {'type': 'terminal', 'value': self.alias, 'cwd': cwd[:200]},
        }

    def snapshot(self):
        with self.lock:
            return {'id': self.id, 'label': self.label, 'alias': self.alias,
                    'machine': self.machine, 'status': self.status, 'hint': self.hint,
                    'claudeLive': self.live, 'fetchedAt': self.fetched,
                    'items': list(self.items)}

    def tick(self, now=None):
        """Advance the schedule and poll once when due (called from the host thread)."""
        now = time.time() if now is None else now
        if now < self.nextAt:
            return
        ok = self.poll()
        delay = HOST_POLL if ok else min(HOST_BACKOFF, HOST_POLL * (2 ** max(0, self.failures - 1)))
        self.nextAt = now + delay

    def start(self):
        if self.thread:
            return self
        def loop():
            # First probe right away, then keep the interval; never blocks the collector.
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:          # a bug here must not kill the thread
                    self.record_failure('采集远程 %s 时出错（%s）' % (self.id, type(exc).__name__))
                self.stop.wait(min(HOST_POLL, max(1.0, self.nextAt - time.time())))
        self.thread = threading.Thread(target=loop, name='remote-'+self.id, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()


def make_hosts(config=None, base=None, script=None, runner=None):
    """Servers from config.json. `remote.enabled` is the master switch."""
    config = config or load_config(Path(base or Path.home()))
    remote = config.get('remote') or {}
    if not remote.get('enabled'):
        return []
    # 配置可能来自手改的文件或界面的半成品：这里再过一遍校验，坏条目直接跳过。
    return [RemoteHost(h, script=script or remote_script(), runner=runner)
            for h in normalise_hosts(remote.get('hosts'))]


def split_remote(hosts):
    """Remote tasks in the same three buckets as local ones."""
    running, done, pending = [], [], []
    for host in hosts:
        for item in host.snapshot()['items']:
            if item.get('state') == 'running':
                running.append(item)
            elif item.get('state') == 'success':
                done.append(item)
            else:
                pending.append(item)
    return running, done, pending


def running_apps():
    alive = {a: False for a in AGENT_LABELS}
    try:
        commands = subprocess.run(['/bin/ps', '-axww', '-o', 'command='], capture_output=True, text=True, errors='replace', timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return alive
    for line in commands.splitlines():
        low = line.lower()
        if '.app/contents/macos/' in low and '/frameworks/' not in low:
            if 'chatgpt.app/' in low or 'codex.app/' in low:
                alive['chatgpt'] = True
            elif 'claude.app/' in low:
                alive['claude'] = True
            elif 'deepseek' in low:
                alive['deepseek'] = True
        if re.search(r'(^|/)codex(?:\s|$)', line) or '@openai/codex' in low:
            alive['chatgpt'] = True
    return alive


def collect_state(readers, now, alive=None, hosts=None):
    alive = running_apps() if alive is None else alive
    running, done, pending, statuses = [], [], [], {}
    for reader in readers:
        r, d, p, s = reader.collect(now, alive.get(reader.agent, False))
        running.extend(r); done.extend(d); pending.extend(p)
        statuses.setdefault(reader.agent, []).append(s)
    sources = {'chatgpt':'Codex 会话记录', 'claude':'Claude 本地任务记录', 'deepseek':'DeepSeek 会话事件'}
    agents = {}
    for a in AGENT_LABELS:
        states = statuses.get(a, [])
        status = 'partial' if 'partial' in states else 'ok' if 'ok' in states else 'missing'
        hint = '未找到本地任务记录' if status == 'missing' else '部分记录暂时无法读取' if status == 'partial' else ''
        if a == 'claude':
            hint = (hint+'；' if hint else '')+'支持保存到本机的任务，云端普通聊天暂未接入'
        agents[a] = {'label':AGENT_LABELS[a], 'open':alive.get(a, False), 'status':status, 'source':sources[a], 'hint':hint}
    def unique(items, key):
        return sorted({i['id']:i for i in items}.values(), key=lambda i:-i.get(key, 0))
    host_states, remote_running, remote_done, remote_pending = [], [], [], []
    if hosts:
        remote_running, remote_done, remote_pending = split_remote(hosts)
        for host in hosts:
            snap = host.snapshot()
            snap.pop('items', None)
            host_states.append(snap)
    return {'version':4, 'agents':agents, 'hosts':host_states,
            'running':unique(running+remote_running, 'start'),
            'done':unique(done+remote_done, 'time'),
            'pending':unique(pending+remote_pending, 'time'),
            'accessMode':'local-and-ssh' if hosts else 'local-sessions'}


def write_atomic(path, text):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(temp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--state-dir', default=str(Path.home()/'Library/Application Support/AI桌宠'))
    ap.add_argument('--parent-pid', type=int, default=0)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--no-ssh', action='store_true', help='只读本机记录，不连接服务器')
    ap.add_argument('--check-ssh', action='store_true', help='连一次每台服务器并把结果打印出来')
    ap.add_argument('--dump-config', action='store_true', help='打印当前配置（含默认值），供界面读取')
    ap.add_argument('--set-config', default='', help='用这份 JSON 覆盖配置并写盘')
    args = ap.parse_args()
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    configure_logging(state_dir)
    config = load_config(state_dir)
    if args.dump_config:
        print(json.dumps(config, ensure_ascii=False, sort_keys=True))
        return 0
    if args.set_config:
        # 界面保存时走这里：先在 Python 侧校验，再落盘，避免两套规则不一致。
        try:
            incoming = json.loads(args.set_config)
        except ValueError:
            print('{"error": "not-json"}')
            return 2
        config = save_config(state_dir, incoming)
        print(json.dumps(config, ensure_ascii=False, sort_keys=True))
        return 0
    save_config(state_dir, config)      # make sure a complete config.json exists
    readers = make_readers(config=config)
    hosts = [] if args.no_ssh else make_hosts(config)
    if args.check_ssh:
        return check_ssh(hosts)
    for host in hosts:
        host.start()           # each probe runs in its own thread; the loop never waits
    last_text = None
    config_mtime = _config_mtime(state_dir)
    try:
        while not args.parent_pid or os.getppid() == args.parent_pid:
            # 设置页保存后，config.json 会变；这里发现变化就重建 readers 和主机线程，
            # 不用重启桌宠。主机的连接状态延续下来，不会因为改一处设置而闪成离线。
            mtime = _config_mtime(state_dir)
            if mtime != config_mtime:
                config_mtime = mtime
                fresh = load_config(state_dir)
                if fresh != config:
                    config = fresh
                    readers = make_readers(config=config)
                    hosts = _rebuild_hosts(hosts, config, args)
            state = collect_state(readers, time.time(), hosts=hosts)
            text = json.dumps(state, ensure_ascii=False, sort_keys=True)
            if args.once:
                print(text)
                return 0
            if text != last_text:
                write_atomic(state_dir/'state.json', text)
                last_text = text
            # Liveness is separate, so the UI distinguishes no tasks from a dead collector.
            write_atomic(state_dir/'health.json', json.dumps({'updatedAt':time.time(), 'pid':os.getpid()}))
            time.sleep(LOOP_SECONDS)
    finally:
        for host in hosts:
            host.close()
    return 0


def _config_mtime(state_dir):
    try:
        return (config_path(state_dir)).stat().st_mtime_ns
    except OSError:
        return 0


def _rebuild_hosts(hosts, config, args):
    """Apply a changed config to the running host threads without dropping their state."""
    old = {h.id: h for h in hosts}
    for host in hosts:
        host.close()
    if args.no_ssh:
        return []
    wanted = make_hosts(config)
    result = []
    for host in wanted:
        previous = old.get(host.id)
        if previous is not None and previous.alias == host.alias:
            # 同一台机器只是改了显示名或顺序：沿用原来的线程，保留上次读到的数据。
            previous.label = host.label
            previous.script = host.script
            result.append(previous)
        else:
            result.append(host.start())
    return result


def check_ssh(hosts, stream=None):
    """`--check-ssh`：同步连一次，报告每台机器读到什么。只用于自检和排错。"""
    stream = stream or sys.stdout
    if not hosts:
        print('没有配置服务器（在面板 ⚙ 里添加，或编辑 config.json）', file=stream)
        return 1
    worst = 0
    for host in hosts:
        started = time.time()
        ok = host.poll()
        snap = host.snapshot()
        elapsed = time.time()-started
        if not ok:
            worst = 1
            print('✗ %s（%s）%.1f 秒：%s' % (snap['label'], snap['alias'], elapsed, snap['hint']), file=stream)
            continue
        states = {}
        for item in snap['items']:
            states[item['state']] = states.get(item['state'], 0)+1
        detail = '，'.join('%s %d' % (k, v) for k, v in sorted(states.items())) or '没有活动任务'
        print('✓ %s（%s）%.1f 秒：%s；Claude 在线 %d 个；%s' % (
            snap['label'], snap['alias'], elapsed, snap['machine'] or '未知主机', snap['claudeLive'], detail), file=stream)
    return worst


if __name__ == '__main__':
    sys.exit(main())

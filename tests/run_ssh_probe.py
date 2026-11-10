#!/usr/bin/env python3
"""独立跑一遍远端探针：不需要 SSH，也不需要真服务器。

用法：`python3 tests/run_ssh_probe.py [假数据根目录]`
不给目录时临时造一份与两台服务器结构一致的假家目录，打印解析结果，
再与本机真实数据跑一遍做对照（本机没有远端那样的记录时会提示）。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT/'ssh_remote.py'


def make_fixture(base):
    home = Path(base)
    now = time.time()
    def write(relative, rows):
        path = home/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf-8')
    # Codex：一轮已完成、一轮还在跑
    write('.codex/sessions/2026/11/04/rollout-2026-11-04T09-00-00-01a1aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl', [
        {'timestamp': '2026-11-04T01:00:00Z', 'type': 'session_meta',
         'payload': {'id': 'thread-1', 'thread_source': 'user'}},
        {'timestamp': '2026-11-04T01:00:01Z', 'type': 'event_msg',
         'payload': {'type': 'task_started', 'turn_id': 't1', 'started_at': now-600}},
        {'timestamp': '2026-11-04T01:00:02Z', 'type': 'event_msg',
         'payload': {'type': 'item_completed', 'turn_id': 't1',
                     'item': {'type': 'UserMessage', 'content': [{'type': 'text', 'text': '把日志整理一下'}]}}},
        {'timestamp': '2026-11-04T01:00:09Z', 'type': 'event_msg',
         'payload': {'type': 'task_complete', 'turn_id': 't1', 'completed_at': now-30, 'last_agent_message': '整理好了'}},
        {'timestamp': '2026-11-04T01:00:10Z', 'type': 'event_msg',
         'payload': {'type': 'task_started', 'turn_id': 't2', 'started_at': now-5}},
        {'timestamp': '2026-11-04T01:00:11Z', 'type': 'event_msg',
         'payload': {'type': 'item_completed', 'turn_id': 't2',
                     'item': {'type': 'UserMessage', 'content': [{'type': 'text', 'text': '再跑一遍回归'}]}}},
    ])
    write('.codex/session_index.jsonl', [{'id': 'thread-1', 'thread_name': '整理实验日志'}])
    # Claude Code：注册表说在等权限，转写里只有半轮
    # 用一个真实存在的 pid，让探针把这条注册表当成在线的会话。
    write('.claude/sessions/%d.json' % os.getpid(), [{
        'pid': os.getpid(), 'sessionId': 's-1', 'cwd': '/srv/project/app',
        'startedAt': int((now-3600)*1000), 'name': '实验分析', 'status': 'permission',
        'statusUpdatedAt': int((now-20)*1000), 'kind': 'interactive'}])
    write('.claude/projects/-srv-project-app/s-1.jsonl', [
        {'type': 'user', 'isSidechain': False, 'sessionId': 's-1', 'timestamp': '2026-11-04T01:00:00Z',
         'message': {'role': 'user', 'content': '把读取与清洗拆成两个模块'}},
        {'type': 'assistant', 'isSidechain': False, 'sessionId': 's-1', 'timestamp': '2026-11-04T01:00:05Z',
         'message': {'role': 'assistant', 'stop_reason': 'tool_use', 'content': [{'type': 'text', 'text': '先看日志'}]}},
    ])
    return home


def run(home, window=86400):
    # 探针自己调用 os.path.expanduser('~')，HOME 变了它才看别的目录。
    result = subprocess.run([str(PROBE), 'fixture', str(window), '40'], capture_output=True, text=True,
                            env=dict(os.environ, HOME=str(home)), cwd=str(home))
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit('探针运行失败')
    return json.loads(result.stdout)


def main():
    if len(sys.argv) > 1:
        homes = [Path(sys.argv[1])]
    else:
        tmp = tempfile.TemporaryDirectory(prefix='ai-pet-probe-')
        homes = [make_fixture(tmp.name), Path.home()]
    for home in homes:
        data = run(home)
        label = '假数据' if len(sys.argv) > 1 or home != Path.home() else '本机真实记录'
        print('== %s（%s）' % (label, home))
        print('   机器 %s；Claude 在线 %d 个' % (data.get('machine') or '—',
                                             (data['agents'].get('claude') or {}).get('live', 0)))
        if not data['items']:
            print('   没有活动任务')
        for item in data['items']:
            print('   %-7s %-9s %-16s %s' % (item['agent'], item['state'], item['title'][:16],
                                             (item['prompt'] or item['detail'])[:40]))
        if data.get('errors'):
            print('   错误：%s' % '，'.join(data['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
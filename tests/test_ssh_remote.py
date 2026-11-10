#!/usr/bin/env python3
"""远端探针（ssh_remote.py）与 collector 的 SSH 层测试。不需要真的连服务器。

探针是一个 `#!/bin/sh` + 内嵌 Python 的文件，这里把 `exec python3 - ... <<'EOF'`
之间的正文跑起来，得到和远端完全一致的解析结果。
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_probe():
    """ssh_remote.py 是一个只读的纯标准库脚本，直接作为模块执行。"""
    source = (ROOT/'ssh_remote.py').read_text(encoding='utf-8')
    assert not source.startswith('#!/bin/sh'), '探针必须是纯 Python，才能用 ssh python3 - 送入'
    module = types.ModuleType('ssh_probe')
    exec(compile(source, 'ssh_remote.py', 'exec'), module.__dict__)
    return module


spec = importlib.util.spec_from_file_location('collector', ROOT/'collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
probe = load_probe()


class ProbeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = time.time()

    # ---- 造出与远端真实结构一致的最小目录 -------------------------------

    def write(self, relative, rows):
        # 探针以 home 为根；fixture 里写 ~/.xxx 表示 home 下的相对路径。
        path = self.home/relative.replace('~/', '', 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False)+'\n')
        return path

    def codex_session(self, tid, events, title='测试会话', day='11/04'):
        rows = [{'timestamp': self.iso(2400), 'type': 'session_meta',
                 'payload': {'id': tid, 'thread_source': 'user'}}]
        rows += events
        rows.append({'timestamp': self.iso(2400), 'type': 'event_msg',
                     'payload': {'type': 'thread_name_updated', 'thread_name': title}})
        return self.write('~/.codex/sessions/2026/%s/rollout-2026-11-04T00-00-00-%s.jsonl' % (day, tid), rows)

    def event(self, kind, ts, **payload):
        return {'timestamp': ts, 'type': 'event_msg', 'payload': dict(type=kind, **payload)}

    def collect(self, window=probe.WINDOW):
        return probe.collect('1.17', now=self.now, window=window, home=str(self.home))

    # ---- Codex -----------------------------------------------------------

    def test_codex_running_then_complete_and_error(self):
        self.codex_session('t-1', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-30),
            self.event('item_completed', self.iso(2398), turn_id='a',
                       item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': '把日志整理一下'}]}),
        ])
        out = self.collect()
        item = out['items'][0]
        self.assertEqual((item['agent'], item['state'], item['host']), ('chatgpt', 'running', '1.17'))
        self.assertEqual((item['title'], item['prompt']), ('测试会话', '把日志整理一下'))

        # 结束记录写进来后变成完成，时间用 completed_at。
        self.codex_session('t-1', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-30),
            self.event('item_completed', self.iso(2398), turn_id='a',
                       item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': '把日志整理一下'}]}),
            self.event('task_complete', self.iso(2380), turn_id='a',
                       completed_at=self.now-5, last_agent_message='整理好了'),
        ])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['time'], item['detail']), ('success', self.now-5, '整理好了'))

        self.codex_session('t-1', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-30),
            self.event('task_complete', self.iso(2380), turn_id='a',
                       completed_at=self.now-5, error={'message': 'stream disconnected'}),
        ])
        item = self.collect()['items'][0]
        self.assertEqual(item['state'], 'error')
        self.assertIn('stream disconnected', item['detail'])

    def test_codex_abort_and_quiet_turn_needs_confirmation(self):
        self.codex_session('t-2', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-60),
            self.event('turn_aborted', self.iso(2395), turn_id='a'),
        ])
        self.assertEqual(self.collect()['items'][0]['state'], 'interrupted')

        self.codex_session('t-2', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-3*3600),
        ])
        # 文件也很久没写过了，才算“没有进展”。
        os.utime(next((self.home/'.codex/sessions').rglob('rollout-*.jsonl')), (self.now-3*3600, self.now-3*3600))
        item = self.collect()['items'][0]
        self.assertEqual(item['state'], 'unknown')
        self.assertIn('30 分钟', item['detail'])

    def test_codex_tail_widens_when_the_file_is_all_token_counts(self):
        """一段很长的 token_count 之后，尾部可能没有 task_started，整个会话会消失。

        真实现场：127 上 36MB 的 rollout，最后一次开轮在文件 49% 处，
        固定 4MB 的尾部窗口里一个开轮事件都没有。
        """
        big = []
        # 先写一段足够长的填充，把真正的开轮事件挤到很前面
        for i in range(4200):
            big.append(self.event('token_count', self.iso(3600), payload={'total': i}))
        self.codex_session('t-huge', big + [
            self.event('task_started', self.iso(1800), turn_id='a', started_at=self.now-1800),
            self.event('item_completed', self.iso(1790), turn_id='a',
                       item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': '跑一个很长的任务'}]}),
        ] + [self.event('token_count', self.iso(60), payload={'total': i}) for i in range(3000)])
        items = self.collect()['items']
        self.assertEqual([i['state'] for i in items], ['running'])
        self.assertEqual(items[0]['prompt'], '跑一个很长的任务')

    def test_codex_goal_is_shown_as_the_question(self):
        """Codex 的 goal 模式：设的目标不是 UserMessage，而是 codex_internal_context。

        真实现场：本机一个 goal 会话在跑，面板里提问是空的，
        因为那一轮只有 task_started + response_item，没有 UserMessage。
        """
        self.codex_session('t-goal', [
            self.event('task_started', self.iso(120), turn_id='g1', started_at=self.now-120),
            {'timestamp': self.iso(119), 'type': 'response_item',
             'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text',
                 'text': '<codex_internal_context source="user_goal">\n'
                         'User set the goal: "把服务器上的图片预览修好，先测试再让我装"\n'
                         'User set goal status: "active".\n'
                         '</codex_internal_context>'}]}},
        ])
        item = self.collect()['items'][0]
        self.assertEqual(item['prompt'], '把服务器上的图片预览修好，先测试再让我装')
        self.assertEqual(item['state'], 'running')

    def test_codex_goal_continuation_uses_the_objective_block(self):
        """自动续跑的那一轮用 source="goal"，目标写在 <objective> 里。"""
        self.codex_session('t-goal2', [
            self.event('task_started', self.iso(60), turn_id='g2', started_at=self.now-60),
            {'timestamp': self.iso(59), 'type': 'response_item',
             'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text',
                 'text': '<codex_internal_context source="goal">\n'
                         'Continue working toward the active thread goal.\n\n'
                         '<objective>\n继续把预览问题修完\n</objective>\n'
                         '</codex_internal_context>'}]}},
        ])
        self.assertEqual(self.collect()['items'][0]['prompt'], '继续把预览问题修完')

    def test_claude_tail_widens_past_a_long_tool_loop(self):
        """Claude 的工具循环会写出上千条 tool_result 和 assistant 行，但没有任何提问。

        它们都是 type user/assistant，如果算"开轮"，窗口就不再扩大，
        提问就永远读不到。真实现场：17 上 40MB 的转写，提问在 82.5%，4MB 窗口从 90% 开始。
        """
        rows = []
        # 抛在很前面的真实提问
        rows.append({'type': 'user', 'sessionId': '377a70e3', 'timestamp': self.iso(7200),
                     'message': {'role': 'user', 'content': '把标注结果整理一下'}})
        rows.append({'type': 'assistant', 'sessionId': '377a70e3', 'timestamp': self.iso(7199),
                     'message': {'role': 'assistant', 'stop_reason': 'tool_use',
                                 'content': [{'type': 'text', 'text': '好的'}]}})
        # 数千条工具循环：只有 tool_result 和 assistant，没有真人提问
        for i in range(20000):
            rows.append({'type': 'user', 'sessionId': '377a70e3', 'timestamp': self.iso(3600),
                         'message': {'role': 'user', 'content': [
                             {'type': 'tool_result', 'tool_use_id': 't%d' % i,
                              'content': 'x' * 300}]}})
            rows.append({'type': 'assistant', 'sessionId': '377a70e3', 'timestamp': self.iso(3599),
                         'message': {'role': 'assistant', 'stop_reason': 'tool_use',
                                     'content': [{'type': 'tool_use', 'id': 't%d' % i,
                                                  'name': 'Bash', 'input': {}}]}})
        rows.append({'type': 'assistant', 'sessionId': '377a70e3', 'timestamp': self.iso(60),
                     'message': {'role': 'assistant', 'stop_reason': 'end_turn',
                                 'content': [{'type': 'text', 'text': '整理完了'}]}})
        self.claude_session('377a70e3', rows, name='数据处理')
        os.utime(next((self.home/'.claude/projects').rglob('*.jsonl')), (self.now, self.now))
        item = self.collect()['items'][0]
        self.assertEqual(item['prompt'], '把标注结果整理一下')
        self.assertEqual(item['detail'], '整理完了')

    def test_codex_compact_keeps_the_question_visible(self):
        """compact 之后，提问写在很靠前的位置，尾部只剩结束事件。

        真实现场：127 上 48.7MB 的 rollout，task_started + UserMessage 在 90.1%，
        而 4MB 的尾部窗口从 91.8% 才开始。窗口里只有 task_complete，
        如果把它也算"开轮事件"，窗口就不再扩大，提问永远是空的。
        """
        pad = []
        # 足够长的填充，把开轮事件挤出 4MB 窗口
        for i in range(9000):
            pad.append(self.event('token_count', self.iso(7200), payload={'i': i, 'pad': 'x' * 300}))
        tail_pad = [self.event('token_count', self.iso(120), payload={'i': i, 'pad': 'y' * 300})
                    for i in range(9000)]
        self.codex_session('t-compact', pad + [
            self.event('task_started', self.iso(3600), turn_id='a', started_at=self.now-3600),
            self.event('item_completed', self.iso(3590), turn_id='a',
                       item={'type': 'UserMessage',
                             'content': [{'type': 'text', 'text': '压缩前提的问题'}]}),
            {'timestamp': self.iso(3500), 'type': 'compacted', 'payload': {}},
        ] + tail_pad + [
            self.event('task_complete', self.iso(60), turn_id='a',
                       completed_at=self.now-60, last_agent_message='答完了'),
        ])
        item = self.collect()['items'][0]
        self.assertEqual(item['prompt'], '压缩前提的问题')
        self.assertEqual(item['detail'], '答完了')

    def test_codex_subagent_and_prompt_wrappers_are_ignored(self):
        self.write('~/.codex/sessions/2026/11/04/rollout-x-child.jsonl', [
            {'timestamp': self.iso(2400), 'type': 'session_meta',
             'payload': {'id': 'child', 'thread_source': 'subagent'}},
            self.event('task_started', self.iso(2399), turn_id='r', started_at=self.now-5)])
        self.codex_session('t-3', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-10),
            self.event('item_completed', self.iso(2398), turn_id='a',
                       item={'type': 'UserMessage', 'content': [
                           {'type': 'text', 'text': '<environment_context><cwd>/a/b</cwd></environment_context>'}]}),
            self.event('task_complete', self.iso(2380), turn_id='a', completed_at=self.now-2),
        ])
        items = self.collect()['items']
        self.assertEqual([i['session'] for i in items], ['t-3'])
        self.assertEqual(items[0]['prompt'], '')

    # ---- Claude Code -----------------------------------------------------

    def claude_session(self, sid, rows, pid=0, status='idle', name='实验分析'):
        pid = pid or os.getpid()
        self.write('~/.claude/sessions/%d.json' % pid, [{
            'pid': pid, 'sessionId': sid, 'cwd': '/srv/project/app',
            'startedAt': int((self.now-3600)*1000), 'name': name, 'nameSource': 'user',
            'status': status, 'statusUpdatedAt': int((self.now-30)*1000),
            'entrypoint': 'claude-vscode', 'kind': 'interactive'}])
        self.write('~/.claude/projects/-srv-project-app/%s.jsonl' % sid, rows)

    def iso(self, seconds_ago):
        """相对 now 的 ISO 时间；写死日期会让“安静多久”算成几小时。"""
        return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(self.now-seconds_ago))

    def user(self, text, ago, sid='s-1'):
        ts = ago if isinstance(ago, str) else self.iso(ago)
        return {'type': 'user', 'isSidechain': False, 'sessionId': sid, 'timestamp': ts,
                'message': {'role': 'user', 'content': text}}

    def assistant(self, text, ago, stop='tool_use', sid='s-1'):
        ts = ago if isinstance(ago, str) else self.iso(ago)
        return {'type': 'assistant', 'isSidechain': False, 'sessionId': sid, 'timestamp': ts,
                'message': {'role': 'assistant', 'stop_reason': stop, 'content': [{'type': 'text', 'text': text}]}}

    def permission_scenario(self):
        """注册表说 permission、转录停在 tool_use：应该显示“等你确认”。"""
        self.claude_session('s-1', [self.user('删掉旧数据', 300),
                                    self.assistant('我需要确认', 290)],
                            status='permission')
        os.utime(next((self.home/'.claude/projects').rglob('*.jsonl')), (self.now, self.now))
        return self.collect()['items'][0]

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_agent_waiting_for_permission_is_marked_waiting(self, _alive):
        item = self.permission_scenario()
        self.assertEqual(item['state'], 'waiting')
        self.assertIn('批准', item['detail'])   # 面板标签叫“待批准”

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_user_interrupt_is_marked_interrupted(self, _alive):
        self.claude_session('s-1', [
            self.user('跑个长任务', 600),
            self.assistant('开始跑', 590),
            self.user('[Request interrupted by user]', 580)])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('interrupted', '跑个长任务'))

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=False)
    def test_killed_process_with_dead_registry_is_interrupted(self, _alive):
        """进程没了、注册表也旧了、转录两小时没动：不能一直显示运行中。"""
        self.claude_session('s-1', [self.user('处理大批量数据', 2*3600),
                                    self.assistant('处理中', 2*3600-10)],
                            status='busy')
        reg = next((self.home/'.claude/sessions').glob('*.json'))
        rec = json.loads(reg.read_text())
        rec['statusUpdatedAt'] = int((self.now-3600)*1000)
        rec['updatedAt'] = int((self.now-3600)*1000)
        reg.write_text(json.dumps(rec))
        item = self.collect()['items'][0]
        self.assertEqual(item['state'], 'interrupted')
        self.assertIn('进程', item['detail'])

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_registry_decides_running_and_waiting(self, _alive):
        rows = [self.user('跑一下推理', 120),
                self.assistant('好的', 110)]
        for status, expected in (('busy', 'running'), ('working', 'running'), ('compacting', 'running'),
                                 ('permission', 'waiting'), ('waiting', 'waiting'), ('blocked', 'waiting')):
            self.claude_session('s-1', rows, status=status)
            item = self.collect()['items'][0]
            self.assertEqual(item['state'], expected, status)
            self.assertEqual(item['title'], '实验分析')

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_finished_turn_and_pending_turn(self, _alive):
        finished = [self.user('整理周报', 400),
                    self.assistant('周报写好了', 300, stop='end_turn')]
        self.claude_session('s-1', finished)
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['detail']), ('success', '周报写好了'))

        # 用户又提了新要求，还没写出 end_turn：这一轮在跑。
        pending = finished + [self.user('顺便看下日志', 20)]
        self.claude_session('s-1', pending)
        self.assertEqual(self.collect()['items'][0]['state'], 'running')

        # 同样的记录，但已经安静超过 30 分钟：不能一直显示运行中。
        self.claude_session('s-1', pending)
        item = probe.collect('1.17', now=self.now+40*60, window=probe.WINDOW, home=str(self.home))['items'][0]
        self.assertEqual(item['state'], 'interrupted')
        self.assertIn('进程', item['detail'])

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_interrupt_and_tool_results_are_not_prompts(self, _alive):
        self.claude_session('s-1', [
            self.user('做一份周报', 300),
            {'type': 'user', 'isSidechain': False, 'sessionId': 's-1', 'timestamp': self.iso(290),
             'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'ok'}]}},
            self.user('[Request interrupted by user]', 280),
        ])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('interrupted', '做一份周报'))

    def test_claude_dead_pid_is_not_running(self):
        # 进程已经不在：注册表记录残留，不能据此一直显示运行中。
        self.claude_session('s-1', [self.user('继续', 3*3600+10),
                                    self.assistant('好的', 3*3600)], status='busy')
        os.utime(next((self.home/'.claude/projects').rglob('*.jsonl')), (self.now-3*3600, self.now-3*3600))
        reg = next((self.home/'.claude/sessions').glob('*.json'))
        rec = json.loads(reg.read_text())
        rec['statusUpdatedAt'] = int((self.now-3*3600)*1000)   # 注册表也是旧的
        rec['updatedAt'] = int((self.now-3*3600)*1000)
        reg.write_text(json.dumps(rec))
        with unittest.mock.patch.object(probe, 'pid_alive', return_value=False):
            item = self.collect()['items'][0]
        self.assertEqual(item['state'], 'interrupted')
        self.assertFalse(probe.pid_alive(2 ** 22 + 12345))
        # 注册表也是旧的（claude_session 写的是 30 秒前），所以不算活着；
        # 但“安静”以转录最后事件算，这里已 3 小时没动 → 中断而不是运行中。

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_start_is_this_exchange_not_the_process(self, _alive):
        # 会话进程 1 小时前就开着（startedAt），刚刚才提问：应显示刚开始，不是 1 小时。
        self.claude_session('s-1', [self.user('上一个问题', 1800),
                                    self.assistant('答完了', 1700, stop='end_turn'),
                                    self.user('把这两个问题记录下来', 20),
                                    self.assistant('好的', 10)], status='busy')
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('running', '把这两个问题记录下来'))
        self.assertAlmostEqual(item['start'], self.now-20, delta=1)

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_prompt_typed_mid_task_joins_the_same_task(self, _alive):
        rows = [self.user('先修登录', 300),
                self.assistant('在改', 250),
                self.user('顺便把日志也看一下', 200),      # 还没答完时追加
                self.assistant('两件都处理好了', 100, stop='end_turn')]
        self.claude_session('s-1', rows)
        items = self.collect()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['state'], items[0]['prompt']), ('success', '先修登录'))
        self.assertEqual((items[0]['followups'], items[0]['followupCount']), (['顺便把日志也看一下'], 1))
        self.assertAlmostEqual(items[0]['start'], self.now-300, delta=1)
        # 还在跑的时候也是同一个任务、第一个问题，追加的也显示。
        self.claude_session('s-1', rows[:3], status='busy')
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt'], item['followups']), ('running', '先修登录', ['顺便把日志也看一下']))

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_queued_command_attachment_is_a_followup(self, _alive):
        # Claude Code 把“干活时你又发的消息”存成 queued_command 附件，不是 user 行。
        def queued(text, ago, mode=None):
            att = {'type': 'queued_command', 'prompt': text}
            if mode:
                att['commandMode'] = mode
            return {'type': 'attachment', 'isSidechain': False, 'sessionId': 's-1', 'timestamp': self.iso(ago), 'attachment': att}
        self.claude_session('s-1', [self.user('先修登录', 300),
                                    self.assistant('在改', 250),
                                    queued([{'type': 'text', 'text': '顺便看下日志'}], 200),
                                    queued('顺便看下日志', 199),                       # 同一条被重复写入
                                    queued('后台任务完成', 150, 'task-notification'),  # 系统通知，不是你问的
                                    self.assistant('两件都好了', 100, stop='end_turn')])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('success', '先修登录'))
        self.assertEqual((item['followups'], item['followupCount']), (['顺便看下日志'], 1))

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_code_own_rows_are_not_followups(self, _alive):
        meta = dict(self.user('技能展开的长说明', 240), isMeta=True)
        summary = dict(self.user('This session is being continued from a previous conversation. Summary...', 230),
                       isCompactSummary=True)
        caveat = self.user('Caveat: The messages below were generated by the user while running local commands.', 220)
        self.claude_session('s-1', [self.user('先修登录', 300), self.assistant('在改', 250),
                                    meta, summary, caveat,
                                    self.user('<local-command-stdout>ok</local-command-stdout>', 215),
                                    self.user('顺便看下日志', 200),
                                    self.assistant('好了', 100, stop='end_turn')])
        item = self.collect()['items'][0]
        self.assertEqual((item['prompt'], item['followups'], item['followupCount']), ('先修登录', ['顺便看下日志'], 1))

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_interrupt_still_counts_even_if_flagged_meta(self, _alive):
        stop = dict(self.user('[Request interrupted by user]', 280), isMeta=True)
        self.claude_session('s-1', [self.user('跑个长任务', 300), self.assistant('开始跑', 290), stop])
        self.assertEqual(self.collect()['items'][0]['state'], 'interrupted')

    def codex_question(self, name, call, ago, title='用 A 还是 B？'):
        args = {'questions': [{'title': title, 'options': ['A', 'B']}]} if name.endswith('_async') else \
               {'questions': [{'id': 'q1', 'header': '方案', 'question': title, 'options': [{'label': 'A', 'description': ''}]}]}
        return {'timestamp': self.iso(ago), 'type': 'response_item',
                'payload': {'type': 'function_call', 'name': name, 'call_id': call, 'arguments': json.dumps(args, ensure_ascii=False),
                            'internal_chat_message_metadata_passthrough': {'turn_id': 'a'}}}

    def codex_output(self, call, ago, output='{"accepted":true}'):
        return {'timestamp': self.iso(ago), 'type': 'response_item',
                'payload': {'type': 'function_call_output', 'call_id': call, 'output': output}}

    def test_codex_remote_blocking_question_waits_until_answered(self):
        said = self.event('item_completed', self.iso(89), turn_id='a',
                          item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': '先修登录'}]})
        rows = [self.event('task_started', self.iso(90), turn_id='a', started_at=self.now-90), said,
                self.codex_question('request_user_input', 'call_1', 80)]
        self.codex_session('t-1', rows)
        os.utime(next((self.home/'.codex/sessions').rglob('rollout-*.jsonl')), (self.now-3600, self.now-3600))
        item = self.collect()['items'][0]                   # an hour of quiet: still waiting on you, not 待确认
        self.assertEqual((item['state'], item['detail'], item['prompt']), ('question', '等你回答：用 A 还是 B？', '先修登录'))
        self.codex_session('t-1', rows + [self.codex_output('call_1', 20, '{"answers":{"q1":{"answers":["A"]}}}')])
        self.assertEqual(self.collect()['items'][0]['state'], 'running')

    def test_codex_remote_async_question_stays_open_after_the_turn_ends(self):
        said = lambda text, ago, tid: self.event('item_completed', self.iso(ago), turn_id=tid,
                                                 item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': text}]})
        rows = [self.event('task_started', self.iso(90), turn_id='a', started_at=self.now-90), said('先修登录', 89, 'a'),
                self.codex_question('request_user_input_async', 'call_2', 80), self.codex_output('call_2', 79),
                self.event('task_complete', self.iso(60), turn_id='a', completed_at=self.now-60, last_agent_message='请回答上面的问题')]
        self.codex_session('t-1', rows)
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['detail']), ('question', '等你回答：用 A 还是 B？'))
        # Your reply arrives as a new turn with the reply envelope: shown as "回答：A", question closed.
        reply = '<send_user_message_question_reply>\n[{"answer":"A","question":"用 A 还是 B？","questionItemId":"x"}]\n</send_user_message_question_reply>'
        self.codex_session('t-1', rows + [self.event('task_started', self.iso(20), turn_id='b', started_at=self.now-20),
                                          said(reply, 19, 'b')])
        items = self.collect()['items']
        self.assertEqual([(i['state'], i['prompt']) for i in items], [('running', '回答：A')])

    def test_codex_remote_new_prompt_after_finish_clears_the_finished_turn(self):
        said = lambda tid, text, ago: self.event('item_completed', self.iso(ago), turn_id=tid,
                                                 item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': text}]})
        self.codex_session('t-1', [
            self.event('task_started', self.iso(90), turn_id='a', started_at=self.now-90), said('a', '先修登录', 89),
            self.event('task_complete', self.iso(60), turn_id='a', completed_at=self.now-60, last_agent_message='好了'),
            self.event('task_started', self.iso(20), turn_id='b', started_at=self.now-20), said('b', '再看下日志', 19)])
        self.assertEqual([(i['state'], i['prompt']) for i in self.collect()['items']], [('running', '再看下日志')])

    def test_codex_remote_message_typed_mid_turn_is_a_followup(self):
        ts = self.iso(2398)
        said = lambda text: self.event('item_completed', ts, turn_id='a',
                                       item={'type': 'UserMessage', 'content': [{'type': 'text', 'text': text}]})
        self.codex_session('t-1', [
            self.event('task_started', self.iso(2399), turn_id='a', started_at=self.now-90),
            said('先修登录'), said('顺便看下日志'),
            self.event('user_message', ts, turn_id='a', message='顺便看下日志'),     # 同一条消息的第二个事件
            self.event('task_complete', ts, turn_id='a', completed_at=self.now-3, last_agent_message='好了')])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('success', '先修登录'))
        self.assertEqual((item['followups'], item['followupCount']), (['顺便看下日志'], 1))

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_new_prompt_after_stop_is_running_not_interrupted(self, _alive):
        self.claude_session('s-1', [self.user('跑个长任务', 600),
                                    self.assistant('开始跑', 590),
                                    self.user('[Request interrupted by user]', 580),
                                    self.user('换个思路再试', 30)])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('running', '换个思路再试'))
        self.assertAlmostEqual(item['start'], self.now-30, delta=1)

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_idle_session_without_recent_transcript_is_not_running(self, _alive):
        self.claude_session('s-1', [self.user('很久以前', 3*86400),
                                    self.assistant('好', 3*86400-10, stop='end_turn')])
        old = self.now - 3*86400
        os.utime(next((self.home/'.claude/projects').rglob('*.jsonl')), (old, old))
        self.assertEqual(self.collect()['items'], [])

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_question_waits_for_you_even_while_registry_says_busy(self, _alive):
        q = {'questions': [{'question': '用 A 还是 B？', 'header': '方案', 'options': [{'label': 'A'}]}]}
        ask = {'type': 'assistant', 'isSidechain': False, 'sessionId': 's-1', 'timestamp': self.iso(100),
               'message': {'role': 'assistant', 'stop_reason': 'tool_use',
                           'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'AskUserQuestion', 'input': q}]}}
        answer = {'type': 'user', 'isSidechain': False, 'sessionId': 's-1', 'timestamp': self.iso(20),
                  'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'A'}]}}
        rows = [self.user('先修登录', 300), self.assistant('先问一下', 200), ask]
        self.claude_session('s-1', rows, status='busy')
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['detail'], item['prompt']), ('question', '等你回答：用 A 还是 B？', '先修登录'))
        self.claude_session('s-1', rows + [answer], status='busy')
        self.assertEqual(self.collect()['items'][0]['state'], 'running')
        # The process is gone: an unanswered question can no longer be answered there.
        self.claude_session('s-1', rows, status='busy')
        with unittest.mock.patch.object(probe, 'pid_alive', return_value=False):
            reg = next((self.home/'.claude/sessions').glob('*.json'))
            rec = json.loads(reg.read_text()); rec['statusUpdatedAt'] = int((self.now-3600)*1000); reg.write_text(json.dumps(rec))
            self.assertNotEqual(self.collect()['items'][0]['state'], 'question')

    @unittest.mock.patch.object(probe, 'pid_alive', return_value=True)
    def test_claude_subagent_rows_do_not_replace_the_prompt(self, _alive):
        self.claude_session('s-1', [
            self.user('主线问题', 300),
            {'type': 'user', 'isSidechain': True, 'sessionId': 's-1', 'timestamp': self.iso(298),
             'message': {'role': 'user', 'content': '子 agent 的内部提问'}},
            self.assistant('回答', 290, stop='end_turn'),
        ])
        item = self.collect()['items'][0]
        self.assertEqual((item['state'], item['prompt']), ('success', '主线问题'))


class RemoteCollectorChecks(unittest.TestCase):
    """collector 的 SSH 层：排队、失败退避、状态合并，全部用假 runner。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.script = ROOT/'ssh_remote.py'
        self.now = time.time()

    def payload(self, items, machine='gpu-box-01'):
        return json.dumps({'host': '1.17', 'machine': machine, 'now': self.now,
                           'agents': {'claude': {'status': 'ok', 'live': 2}},
                           'items': items, 'errors': []}).encode()

    def item(self, **kw):
        base = {'agent': 'chatgpt', 'host': '1.17', 'session': 's-1', 'title': '远程任务',
                'prompt': '帮我看下日志', 'state': 'running', 'start': self.now-60,
                'time': self.now-5, 'resumed': False}
        base.update(kw)
        return base

    def test_successful_probe_merges_remote_tasks(self):
        host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'},
                            script=self.script, runner=lambda: self.payload([
                                self.item(),
                                self.item(session='s-2', state='success', title='已完成的远程任务'),
                                self.item(session='s-3', state='interrupted', agent='claude')]))
        self.assertTrue(host.poll())
        running, done, pending = c.split_remote([host])
        self.assertEqual((len(running), len(done), len(pending)), (1, 1, 1))
        snap = host.snapshot()
        self.assertEqual((snap['status'], snap['claudeLive'], snap['machine']),
                         ('ok', 2, 'gpu-box-01'))
        remote = running[0]
        self.assertTrue(remote['id'].startswith('ssh:1.17:chatgpt:'))
        self.assertEqual(remote['open']['type'], 'terminal')
        self.assertEqual(remote['open']['value'], 'gpu-box')

    def test_failure_keeps_last_items_and_marks_offline_then_stale(self):
        answers = [self.payload([self.item()])]
        def runner():
            if answers:
                return answers.pop()
            raise RuntimeError('SSH 认证失败，请检查密钥或服务器是否允许登录')
        host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'},
                            script=self.script, runner=runner)
        now = self.now
        host.tick(now)
        self.assertEqual(host.snapshot()['status'], 'ok')
        self.assertAlmostEqual(host.nextAt, now+c.HOST_POLL, places=1)   # 成功后按间隔再拉
        host.tick(now+1)                                                  # 未到点：不碰网络
        self.assertEqual(host.snapshot()['status'], 'ok')
        host.tick(now+c.HOST_POLL)
        snap = host.snapshot()
        self.assertEqual(snap['status'], 'stale')          # 有旧数据：冻结，不消失
        self.assertIn('认证失败', snap['hint'])
        self.assertEqual(len(snap['items']), 1)
        self.assertGreater(host.nextAt, now+c.HOST_POLL)                  # 失败后退避
        delays = [host.nextAt-now-c.HOST_POLL]
        for _ in range(5):                                  # 连续失败：间隔只增不减，且封顶
            at = host.nextAt
            host.tick(at)
            delays.append(host.nextAt-at)
        self.assertEqual(delays, sorted(delays))
        self.assertLessEqual(max(delays), c.HOST_BACKOFF)
        self.assertEqual(len(host.snapshot()['items']), 1)

    def test_timeout_and_garbage_payload_are_offline_not_crash(self):
        for runner in (lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired('ssh', 45)),
                       lambda: b'not json at all'):
            host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'},
                                script=self.script, runner=runner)
            self.assertFalse(host.poll())
            snap = host.snapshot()
            self.assertEqual((snap['status'], snap['items']), ('offline', []))

    def test_remote_window_and_limit_reach_the_probe(self):
        seen = {}
        class Proc:
            returncode = 0
            stdout = b'{"machine":"m","agents":{},"items":[]}'
            stderr = b''
        def fake_run(argv, **kwargs):
            seen['args'] = argv[-1]
            return Proc()
        host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'}, script=self.script)
        with patch.object(c.subprocess, 'run', side_effect=fake_run):
            self.assertTrue(host.poll())
        self.assertTrue(seen['args'].startswith('python3 - '))
        hostid, window, limit = seen['args'].split()[2:]
        self.assertEqual((hostid, int(window), int(limit)),
                         ('1.17', c.REMOTE_WINDOW, c.REMOTE_MAX_ITEMS))
        self.assertEqual(c.REMOTE_WINDOW, c.WINDOW)

    def test_duplicate_and_unknown_items_are_dropped(self):
        host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'},
                            script=self.script, runner=lambda: self.payload([
                                self.item(), self.item(), self.item(agent='unknown'),
                                self.item(state='weird', session='s-9')]))
        self.assertTrue(host.poll())
        states = [i['state'] for i in host.snapshot()['items']]
        self.assertEqual(len(states), 2)
        self.assertIn('unknown', states)

    def test_state_reports_hosts_and_merges_buckets(self):
        host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'},
                            script=self.script, runner=lambda: self.payload([self.item(), self.item(session='s-2', state='success')]))
        host.poll()
        state = c.collect_state([], self.now, alive={'chatgpt': False}, hosts=[host])
        self.assertEqual(state['version'], 4)
        self.assertEqual(state['accessMode'], 'local-and-ssh')
        self.assertEqual([h['id'] for h in state['hosts']], ['1.17'])
        self.assertNotIn('items', state['hosts'][0])
        self.assertEqual((len(state['running']), len(state['done'])), (1, 1))
        # 没有服务器时保持原来的行为。
        local = c.collect_state([], self.now, alive={'chatgpt': False})
        self.assertEqual((local['version'], local['accessMode'], local['hosts']), (4, 'local-sessions', []))

    def test_host_labels_stay_short(self):
        self.assertEqual(c.short_label('192.168.0.17'), '17')          # IP 取最后一段
        self.assertEqual(c.short_label('192.168.0.127'), '127')
        self.assertEqual(c.short_label('gpu-box'), 'gpu-box')          # 没有点就整名保留
        self.assertEqual(c.short_label('a.b.example.com'), 'com')
        # 没写 label 时用短名；写了就用用户的。
        hosts = c.normalise_hosts([{'id': '10.0.0.9', 'alias': '10.0.0.9'},
                                   {'id': 'x', 'alias': 'h', 'label': '算力机'}])
        self.assertEqual([h['label'] for h in hosts], ['9', '算力机'])

    def test_host_list_rejects_unsafe_aliases_and_disabled_entries(self):
        hosts = c.normalise_hosts([
            {'id': 'a', 'alias': 'gpu-box'},
            {'id': 'b', 'alias': '-oProxyCommand=evil'},
            {'id': 'c', 'alias': 'host with space'},
            {'id': 'd', 'alias': 'user@example.com:2222', 'label': '示例'},
            {'id': 'e', 'alias': '10.0.0.99', 'enabled': False},
            {'id': 'a', 'alias': '10.0.0.19'},                        # 重复 id 只留第一个
        ])
        # 显式给了 id 就用它当短名；只有 id 缺失时才从 alias 里取最后一段。
        self.assertEqual([(h['id'], h['alias'], h['label']) for h in hosts],
                         [('a', 'gpu-box', 'a'), ('d', 'user@example.com:2222', '示例')])
        self.assertEqual(c.ssh_alias({'alias': '-oProxyCommand=x'}), '')

    # --- 配置：发布到 GitHub 后，这些字段都由用户自己填 --------------------

    def test_config_defaults_when_missing_or_broken(self):
        # 全新安装必须没有任何服务器：发布出去的包不能带别人的机器。
        empty = c.default_config()
        self.assertEqual(empty['remote'], {'enabled': False, 'hosts': []})
        self.assertEqual(empty['local'], {'codex': '', 'claude': '', 'deepseek': ''})
        (self.base/'config.json').write_text('{broken')
        broken = c.load_config(self.base)
        self.assertEqual(broken['remote'], {'enabled': False, 'hosts': []})

    def test_config_is_only_read_from_config_json(self):
        # No migration: config.json (or defaults) is the only source of settings.
        self.assertFalse(hasattr(c, 'migrate_legacy_servers'))
        self.assertEqual(c.load_config(self.base)['remote'], {'enabled': False, 'hosts': []})
        c.save_config(self.base, {'remote': {'enabled': True, 'hosts': [{'id': 'x', 'alias': 'gpu-box'}]}})
        self.assertEqual([h['alias'] for h in c.load_config(self.base)['remote']['hosts']], ['gpu-box'])

    def test_config_save_sanitises_every_field(self):
        saved = c.save_config(self.base, {
            'local': {'codex': '/data/codex', 'claude': '', 'deepseek': 'relative'},
            'remote': {'enabled': True, 'hosts': [
                {'id': 'a', 'alias': '10.0.0.1', 'label': '一号'},
                {'id': 'b', 'alias': '-oProxyCommand=evil'},
                {'id': 'c', 'alias': 'with space'}]},
            'appearance': {'scale': 9},
            'junk': 'dropped'})
        self.assertEqual([h['alias'] for h in saved['remote']['hosts']], ['10.0.0.1'])
        self.assertEqual(saved['local']['deepseek'], '')      # 相对路径不要
        self.assertEqual(saved['appearance']['scale'], 1.8)   # 超出范围夹紧
        self.assertNotIn('junk', saved)
        # 写到磁盘上的内容与返回值一致，权限只有自己能读。
        on_disk = json.loads((self.base/'config.json').read_text())
        self.assertEqual(on_disk, saved)
        self.assertEqual((self.base/'config.json').stat().st_mode & 0o777, 0o600)

    def test_config_hosts_drive_the_remote_switch(self):
        off = c.make_hosts({'remote': {'enabled': False, 'hosts': [{'alias': 'a.example'}]}})
        self.assertEqual(off, [])                                    # 总开关关掉就不连
        on = c.make_hosts({'remote': {'enabled': True, 'hosts': [{'alias': 'a.example'}]}},
                          script=self.script, runner=lambda: b'{"items": []}')
        self.assertEqual([h.alias for h in on], ['a.example'])

    def test_local_paths_can_be_overridden(self):
        roots = c.local_roots({'local': {'codex': '/x/codex'}}, base=Path('/home/u'))
        self.assertEqual(roots['codex'], Path('/x/codex'))
        self.assertEqual(roots['claude'], Path('/home/u')/'Library/Application Support/Claude')
        readers = c.make_readers(base=Path('/home/u'), config={'local': {'codex': '/x/codex'}})
        self.assertEqual(str(readers[0].root), '/x/codex')

    def test_ssh_alias_never_starts_with_a_dash(self):
        # `-oProxyCommand=...` 会被 ssh 当成选项，必须挡在参数之外。
        self.assertEqual(c.ssh_alias({'alias': '-oProxyCommand=evil'}), '')
        self.assertEqual(c.ssh_alias({'alias': '-bad'}), '')
        self.assertEqual(c.ssh_alias({'alias': 'ok-host'}), 'ok-host')

    def test_ssh_command_is_argv_safe_and_read_only(self):
        captured = {}
        class Proc:
            returncode = 0
            stdout = b'{"machine":"m","agents":{},"items":[]}'
            stderr = b''
        def fake_run(argv, **kwargs):
            captured['argv'] = argv
            captured['kwargs'] = kwargs
            return Proc()
        host = c.RemoteHost({'id': '1.17', 'alias': 'gpu-box', 'label': '1.17'}, script=self.script)
        with patch.object(c.subprocess, 'run', side_effect=fake_run):
            self.assertTrue(host.poll())
        argv = captured['argv']
        self.assertEqual(argv[0], '/usr/bin/ssh')
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertEqual(argv[-2], 'gpu-box')
        self.assertEqual(argv[-1], 'python3 - 1.17 %d %d' % (c.REMOTE_WINDOW, c.REMOTE_MAX_ITEMS))
        self.assertIsNotNone(captured['kwargs']['input'])
        # 探针正文里不能出现写文件或凭据路径。
        body = self.script.read_text(encoding='utf-8')
        # 只看代码，不看说明文字（注释里会声明“不读凭据”）。
        code = body.split('"""', 2)[-1]
        for forbidden in ('auth.json', 'settings.json', '.claude.json', "'w'", 'os.remove', 'os.mkdir', 'chmod'):
            self.assertNotIn(forbidden, code, forbidden)
        # 只允许只读打开（r / rb / encoding=...），不出现任何写模式。
        modes = [m.group(1) for m in __import__('re').finditer(r"open\([^)]*?([wa]b?)['\"]", code)]
        self.assertEqual([m for m in modes if 'w' in m or 'a' in m], [])


if __name__ == '__main__':
    unittest.main()
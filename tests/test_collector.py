import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('collector', Path(__file__).parents[1]/'collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CollectorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.now = time.time()

    def write(self, path, rows, compressed=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = ''.join(json.dumps(r)+'\n' for r in rows).encode()
        path.write_bytes(c.zstandard.ZstdCompressor().compress(data) if compressed else data)

    def dsh(self, events, parent=False):
        path = self.base/'work/session-test/session.v4.jsonl.zstd'
        rows = [{'type':'session','id':'session-test', 'delegationDepth':int(parent)}, {'type':'session/title','data':{'title':'测试'}}]+events
        self.write(path, rows, True)
        return c.Reader('deepseek',self.base,'*/session-*/session.v4.jsonl.zstd',c.parse_deepseek)

    def start(self, turn, ts):
        return {'type':'turn/start','time':ts*1000,'data':{'turn':turn}}

    def end(self, turn, ts, reason='completed'):
        return {'type':'turn/end','time':ts*1000,'data':{'turn':turn,'reason':{'kind':reason}}}

    def test_dsh_finished_uses_end_time(self):
        reader=self.dsh([self.start(1,self.now-600),self.end(1,self.now-4)])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p),s),(0,1,0,'ok'))
        self.assertAlmostEqual(d[0]['time'],self.now-4,places=3)

    def test_second_turn_keeps_unread_prior_completion(self):
        reader=self.dsh([self.start(1,self.now-30),self.end(1,self.now-3),self.start(2,self.now-2)])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(1,1,0))
        self.assertTrue(r[0]['id'].endswith(':2'))

    def test_long_running_task_is_not_completed(self):
        reader=self.dsh([self.start(1,self.now-7200)])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d)),(1,0))
        os.utime(next(self.base.glob('*/*/*.zstd')), (self.now-4000, self.now-4000))
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(0,0,1))
        self.assertEqual(p[0]['state'],'unknown')
        self.assertIn('30 分钟', p[0]['detail'])
        os.utime(next(self.base.glob('*/*/*.zstd')), (self.now,self.now))
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(1,0,0))

    def test_errors_aborts_and_subagents_never_increment_done(self):
        for reason in ('error','aborted','max-tokens','unexpected'):
            reader=self.dsh([self.start(1,self.now-3),self.end(1,self.now,reason)])
            r,d,p,s=reader.collect(self.now,True)
            self.assertEqual((len(d),len(p)),(0,1))
        # A step refused by a plugin rule ends the turn normally: finished, with a note.
        reader=self.dsh([self.start(1,self.now-3),self.end(1,self.now,'blocked')])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(d),len(p),d[0]['state']),(1,0,'success'))
        self.assertIn('拦下',d[0]['detail'])
        reader=self.dsh([self.start(1,self.now-3),self.end(1,self.now)],True)
        self.assertEqual(reader.collect(self.now,True)[:3],([],[],[]))

    def test_claude_tool_and_subagent_results_do_not_finish_parent(self):
        path=self.base/'audit.jsonl'
        self.write(path,[
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-3},
            {'type':'result','user_message_uuid':'child','parent_tool_use_id':'tool1','timestamp':self.now,'subtype':'success'},
            {'type':'user','message':{'content':[{'type':'tool_result','content':'ok'}]}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'completed','timestamp':self.now},
        ])
        info=c.parse_claude(path)
        self.assertEqual(list(info['turns']),['u1'])
        self.assertEqual(info['turns']['u1']['end'],0)
        with path.open('a') as f:f.write(json.dumps({'type':'result','user_message_uuid':'u1','subtype':'success','timestamp':self.now})+'\n')
        self.assertEqual(c.parse_claude(path)['turns']['u1']['state'],'success')

    def test_codex_fork_metadata_cannot_override_subagent_identity(self):
        path=self.base/'rollout.jsonl'
        self.write(path,[{'type':'session_meta','payload':{'id':'child','thread_source':'subagent'}},
                         {'type':'session_meta','payload':{'id':'parent','thread_source':'user'}}])
        info=c.parse_codex(path)
        self.assertTrue(info['skip'])
        self.assertEqual(info['id'],'child')

    def test_partial_last_record_does_not_crash_or_finish_task(self):
        path=self.base/'rollout.jsonl'
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
                         {'type':'event_msg','payload':{'type':'task_started','turn_id':'r','started_at':self.now}}])
        with path.open('a') as f:f.write('{"type":"event_msg","payload":')
        self.assertEqual(c.parse_codex(path)['turns']['r']['end'],0)

    def test_codex_aborted_timestamp_stable_across_reloads(self):
        path=self.base/'rollout.jsonl'
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'r','started_at':self.now-3}},
            {'type':'event_msg','timestamp':'2026-11-02T10:30:00Z','payload':{'type':'turn_aborted','turn_id':'r'}}])
        self.assertEqual(c.parse_codex(path)['turns']['r']['end'],1793615400)

    def test_codex_uses_actual_title_and_refreshes_rename_without_new_messages(self):
        path=self.base/'rollout.jsonl'
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
            {'type':'event_msg','payload':{'type':'user_message','message':'很长的原始提问'}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'r','started_at':self.now}}])
        index=self.base/'session_index.jsonl'
        self.write(index,[{'id':'t','thread_name':'制作AI对话管理桌宠'}])
        reader=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex)
        self.assertEqual(reader.collect(self.now,True)[0][0]['title'],'制作AI对话管理桌宠')
        with index.open('a') as f:
            f.write(json.dumps({'id':'t','thread_name':'我的可爱桌宠'})+'\n')
        self.assertEqual(reader.collect(self.now,True)[0][0]['title'],'我的可爱桌宠')
        # A concurrent partial write must not erase an existing name.
        with index.open('a') as f:f.write('{"id":"t","thread_name":')
        self.assertEqual(reader.collect(self.now,True)[0][0]['title'],'我的可爱桌宠')
        index.unlink()
        self.assertEqual(reader.collect(self.now,True)[0][0]['title'],'我的可爱桌宠')

    def test_codex_resumed_turn_hides_old_interruption_and_keeps_prompts_per_turn(self):
        path=self.base/'rollout.jsonl'
        def event(kind, **args):return {'type':'event_msg','timestamp':self.now-2,'payload':dict(type=kind,**args)}
        self.write(path,[{'type':'session_meta','payload':{'id':'thread'}},
            event('task_started',turn_id='old',started_at=self.now-100),
            event('item_completed',turn_id='old',item={'type':'UserMessage','content':[{'type':'text','text':'第一轮问题'}]}),
            event('turn_aborted',turn_id='old'),
            event('task_started',turn_id='new',started_at=self.now-1),
            event('item_completed',turn_id='new',item={'type':'UserMessage','content':[{'type':'text','text':'继续处理图标'}]}),
            event('item_completed',turn_id='new',item={'type':'UserMessage','content':[{'type':'text','text':'<environment_context>runtime</environment_context>'}]})])
        info=c.parse_codex(path)
        self.assertEqual(info['turns']['old']['prompt'],'第一轮问题')
        r,d,p,s=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(1,0,0))
        self.assertEqual(r[0]['prompt'],'继续处理图标')

    def test_deepseek_prompt_does_not_pick_runtime_or_permission_messages(self):
        reader=self.dsh([self.start(1,self.now-10),
            {'type':'user/message','data':{'source':{'kind':'user'},'content':[{'type':'text','text':'请修复任务面板'}]}},
            {'type':'user/message','data':{'source':{'kind':'runtime-context'},'content':[{'type':'text','text':'runtime'}]}},
            self.end(1,self.now-5),self.start(2,self.now-3),
            {'type':'user/message','data':{'source':{'kind':'user'},'content':[{'type':'text','text':'现在更新桌宠'}]}}])
        info=c.parse_deepseek(self.base/'work/session-test/session.v4.jsonl.zstd')
        self.assertEqual(info['turns']['1']['prompt'],'请修复任务面板')
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((r[0]['prompt'],len(d)),('现在更新桌宠',0))   # your new prompt cleared turn 1

    def test_dsh_message_typed_mid_turn_stays_in_that_turn(self):
        def said(text):return {'type':'user/message','data':{'source':{'kind':'user'},'content':[{'type':'text','text':text}]}}
        reader=self.dsh([self.start(1,self.now-60),said('先修登录'),
            {'type':'agent/inbox/spliced','data':{'target':'x','start':0,'inserted':[]}},
            said('顺便看下日志'),self.end(1,self.now-5)])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(0,1,0))
        self.assertEqual((d[0]['prompt'],d[0]['followups'],d[0]['followupCount']),('先修登录',['顺便看下日志'],1))

    def test_codex_message_typed_mid_turn_stays_in_that_turn(self):
        path=self.base/'rollout.jsonl'
        def event(kind, **args):return {'type':'event_msg','timestamp':self.now-2,'payload':dict(type=kind,**args)}
        def said(text):return event('item_completed',turn_id='a',item={'type':'UserMessage','content':[{'type':'text','text':text}]})
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
            event('task_started',turn_id='a',started_at=self.now-90),said('先修登录'),
            said('顺便看下日志'),event('user_message',turn_id='a',message='顺便看下日志'),   # same message, second event
            event('task_complete',turn_id='a',completed_at=self.now-3,last_agent_message='两件都好了')])
        r,d,p,s=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(0,1,0))
        self.assertEqual((d[0]['prompt'],d[0]['start']),('先修登录',self.now-90))
        self.assertEqual((d[0]['followups'],d[0]['followupCount']),(['顺便看下日志'],1))

    def test_followups_keep_the_latest_two_and_count_all(self):
        turn=c.new_turn('a')
        for text in ('第一个问题','追加一','追加二','追加三','追加二'):
            c.keep_first_prompt(turn,text)
        self.assertEqual((turn['prompt'],turn['followups'],turn['followup_count']),('第一个问题',['追加二','追加三'],3))
        # A task without follow-ups carries no extra fields.
        reader=self.dsh([self.start(1,self.now-9),self.end(1,self.now-1)])
        self.assertNotIn('followups',reader.collect(self.now,True)[1][0])

    def test_claude_queued_message_attachment_is_a_followup_not_a_notice(self):
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-60,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-60},
            {'type':'attachment','timestamp':self.now-40,'attachment':{'type':'queued_command','prompt':[{'type':'text','text':'顺便看下日志'}]}},
            {'type':'attachment','timestamp':self.now-39,'attachment':{'type':'queued_command','prompt':'顺便看下日志'}},   # re-serialised copy
            {'type':'attachment','timestamp':self.now-30,'attachment':{'type':'queued_command','commandMode':'task-notification','prompt':'后台任务完成'}}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((r[0]['prompt'],r[0]['followups'],r[0]['followupCount']),('先修登录',['顺便看下日志'],1))

    def test_claude_own_messages_are_not_followups(self):
        # Compaction summary, skill expansion and command output are written by Claude, not by you.
        def own(uid, ago, text, **flags):
            return [{'type':'user','uuid':uid,'timestamp':self.now-ago,'message':{'content':text},**flags},
                    {'type':'command_lifecycle','command_uuid':uid,'state':'queued','timestamp':self.now-ago}]
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-90,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-90},
            *own('s1',80,'This session is being continued from a previous conversation that ran out of context. Summary: ...'),
            *own('s2',70,'Base directory for this skill: /x/y\n# PDF skill'),
            *own('s3',60,'技能展开的长说明',isMeta=True),
            *own('s4',50,'<local-command-stdout>ok</local-command-stdout>'),
            *own('s5',40,'<task-notification><summary>后台任务完成</summary></task-notification>'),
            *own('b',30,'顺便看下日志')])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(len(r),1)
        self.assertEqual((r[0]['prompt'],r[0]['followups'],r[0]['followupCount']),('先修登录',['顺便看下日志'],1))

    def test_claude_transcript_merges_all_absorbed_followups(self):
        rows = [
            {'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'主任务'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300,
             'session_id':'session-test'},
            {'type':'user','uuid':'b','timestamp':self.now-200,'message':{'content':'追加一'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-180,
             'session_id':'session-test'},
            {'type':'user','uuid':'c','timestamp':self.now-100,'message':{'content':'追加二'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'c','state':'started','timestamp':self.now-90,
             'session_id':'session-test'},
        ]
        queue = [
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-300,'sessionId':'session-test','content':'主任务'},
            {'type':'queue-operation','operation':'dequeue','timestamp':self.now-299,'sessionId':'session-test'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-200,'sessionId':'session-test','content':'追加一'},
            {'type':'queue-operation','operation':'remove','timestamp':self.now-180,'sessionId':'session-test','content':'追加一','reason':'absorbed_mid_turn','commandUuid':'b'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-100,'sessionId':'session-test','content':'追加二'},
            {'type':'queue-operation','operation':'remove','timestamp':self.now-90,'sessionId':'session-test','content':'追加二','reason':'absorbed_mid_turn','commandUuid':'c'},
        ]
        reader = self.claude_audit_with_transcript(rows, queue)
        r, d, p, _ = reader.collect(self.now, True)
        self.assertEqual((len(r), len(d), len(p)), (1, 0, 0))
        self.assertEqual((r[0]['prompt'], r[0]['followups'], r[0]['followupCount']),
                         ('主任务', ['追加一', '追加二'], 2))

    def test_claude_transcript_keeps_a_third_queued_followup_with_the_lead(self):
        rows = [
            {'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'主任务'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300,
             'session_id':'session-test'},
            {'type':'user','uuid':'b','timestamp':self.now-200,'message':{'content':'追加一'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-180,
             'session_id':'session-test'},
            {'type':'user','uuid':'c','timestamp':self.now-100,'message':{'content':'追加二'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'c','state':'started','timestamp':self.now-90,
             'session_id':'session-test'},
            {'type':'user','uuid':'d','timestamp':self.now-20,'message':{'content':'追加三'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'d','state':'queued','timestamp':self.now-20,
             'session_id':'session-test'},
        ]
        queue = [
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-300,'sessionId':'session-test','content':'主任务'},
            {'type':'queue-operation','operation':'dequeue','timestamp':self.now-299,'sessionId':'session-test'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-200,'sessionId':'session-test','content':'追加一'},
            {'type':'queue-operation','operation':'remove','timestamp':self.now-180,'sessionId':'session-test','content':'追加一','reason':'absorbed_mid_turn','commandUuid':'b'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-100,'sessionId':'session-test','content':'追加二'},
            {'type':'queue-operation','operation':'remove','timestamp':self.now-90,'sessionId':'session-test','content':'追加二','reason':'absorbed_mid_turn','commandUuid':'c'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-20,'sessionId':'session-test','content':'追加三'},
        ]
        reader = self.claude_audit_with_transcript(rows, queue)
        r, d, p, _ = reader.collect(self.now, True)
        self.assertEqual((len(r), len(d), len(p)), (1, 0, 0))
        self.assertEqual((r[0]['prompt'], r[0]['followups'], r[0]['followupCount']),
                         ('主任务', ['追加二', '追加三'], 3))

    def test_claude_repeated_prompt_text_keeps_a_later_dispatched_task(self):
        rows = [
            {'type':'user','uuid':'a','timestamp':self.now-3000,'message':{'content':'先改配置'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-3000,
             'session_id':'session-test'},
            {'type':'user','uuid':'b','timestamp':self.now-2900,'message':{'content':'继续'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-2880,
             'session_id':'session-test'},
            {'type':'result','user_message_uuid':'a','subtype':'success','timestamp':self.now-2800},
            {'type':'user','uuid':'c','timestamp':self.now-120,'message':{'content':'继续'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'c','state':'started','timestamp':self.now-120,
             'session_id':'session-test'},
        ]
        queue = [
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-3000,'sessionId':'session-test','content':'先改配置'},
            {'type':'queue-operation','operation':'dequeue','timestamp':self.now-3000,'sessionId':'session-test'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-2900,'sessionId':'session-test','content':'继续'},
            {'type':'queue-operation','operation':'remove','timestamp':self.now-2880,'sessionId':'session-test','content':'继续','reason':'absorbed_mid_turn','commandUuid':'b'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-120,'sessionId':'session-test','content':'继续'},
            {'type':'queue-operation','operation':'dequeue','timestamp':self.now-120,'sessionId':'session-test'},
        ]
        reader = self.claude_audit_with_transcript(rows, queue)
        r, d, p, _ = reader.collect(self.now, True)
        self.assertEqual(([i['prompt'] for i in r], d, p), (['继续'], [], []))

    def test_claude_transcript_dequeue_keeps_a_new_task_separate(self):
        rows = [
            {'type':'user','uuid':'a','timestamp':self.now-600,'message':{'content':'旧任务'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-600,
             'session_id':'session-test'},
            {'type':'user','uuid':'b','timestamp':self.now-200,'message':{'content':'新任务'},
             'session_id':'session-test'},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-190,
             'session_id':'session-test'},
        ]
        queue = [
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-600,'sessionId':'session-test','content':'旧任务'},
            {'type':'queue-operation','operation':'dequeue','timestamp':self.now-599,'sessionId':'session-test'},
            {'type':'queue-operation','operation':'enqueue','timestamp':self.now-200,'sessionId':'session-test','content':'新任务'},
            {'type':'queue-operation','operation':'dequeue','timestamp':self.now-190,'sessionId':'session-test'},
        ]
        reader = self.claude_audit_with_transcript(rows, queue)
        r, d, p, _ = reader.collect(self.now, True)
        self.assertEqual([item['prompt'] for item in r], ['新任务'])
        self.assertEqual((d, p), ([], []))

    def test_unclosed_claude_command_does_not_swallow_later_tasks(self):
        # The first command never logs an end; its follow-up starts and finishes, then a new
        # question runs on its own. The new question must stay a separate task.
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-600,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-600},
            {'type':'user','uuid':'b','timestamp':self.now-500,'message':{'content':'顺便看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-400},
            {'type':'command_lifecycle','command_uuid':'b','state':'completed','timestamp':self.now-300},
            {'type':'user','uuid':'c','timestamp':self.now-200,'message':{'content':'换个话题：整理周报'}},
            {'type':'command_lifecycle','command_uuid':'c','state':'started','timestamp':self.now-200}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual([i['prompt'] for i in r],['换个话题：整理周报'])
        self.assertNotIn('followups',r[0])

    def claude_audit_reader(self, rows):
        path = self.base/'sess/abcd1234/audit.jsonl'
        self.write(path, rows)
        return c.Reader('claude', self.base, '*/*/audit.jsonl', c.parse_claude)

    def claude_audit_with_transcript(self, rows, queue_rows, session_id='session-test'):
        path = self.base/'sess/abcd1234/audit.jsonl'
        audit = [dict(row, session_id=row.get('session_id', session_id)) for row in rows]
        self.write(path, audit)
        transcript = path.parent/'.claude/projects/session'/f'{session_id}.jsonl'
        self.write(transcript, queue_rows)
        return c.Reader('claude', self.base, '*/*/audit.jsonl', c.parse_claude)

    def test_new_prompt_retires_a_finished_turn_you_have_seen(self):
        """同一对话里，上一轮结束后你又发了新问题：上一轮相当于看过了，不再占位。"""
        reader = self.claude_audit_reader([
            {'type':'user','uuid':'u1','timestamp':self.now-900,'message':{'content':'问题一'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-900},
            {'type':'result','user_message_uuid':'u1','subtype':'success','result':'答一','timestamp':self.now-800},
            {'type':'user','uuid':'u2','timestamp':self.now-100,'message':{'content':'问题二'}},
            {'type':'command_lifecycle','command_uuid':'u2','state':'started','timestamp':self.now-100},
        ])
        r,d,p,_ = reader.collect(self.now, True)
        self.assertEqual(([i['prompt'] for i in d], [i['prompt'] for i in r]), ([], ['问题二']))

    def test_error_turn_is_retired_after_a_later_claude_prompt(self):
        """Claude 同一会话继续开始新任务后，旧错误已经被处理，不再占位。"""
        reader = self.claude_audit_reader([
            {'type':'user','uuid':'u1','timestamp':self.now-900,'message':{'content':'出错的那轮'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-900},
            {'type':'result','user_message_uuid':'u1','subtype':'error','is_error':True,'result':'挂了','timestamp':self.now-800},
            {'type':'user','uuid':'u2','timestamp':self.now-100,'message':{'content':'新问题'}},
            {'type':'command_lifecycle','command_uuid':'u2','state':'started','timestamp':self.now-100},
        ])
        r,d,p,_ = reader.collect(self.now, True)
        self.assertEqual(([i['prompt'] for i in r], d, p), (['新问题'], [], []))

    def test_queued_message_does_not_retire_the_running_turn(self):
        """上一轮还没答完你就发的消息属于排队，不算“看过”，上一轮要保留。"""
        reader = self.claude_audit_reader([
            {'type':'user','uuid':'u1','timestamp':self.now-900,'message':{'content':'进行中'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-900},
            {'type':'user','uuid':'u2','timestamp':self.now-500,'message':{'content':'排队追问'}},
            {'type':'command_lifecycle','command_uuid':'u2','state':'queued','timestamp':self.now-500},
            {'type':'result','user_message_uuid':'u1','subtype':'success','result':'ok','timestamp':self.now-400},
        ])
        r,d,p,_ = reader.collect(self.now, True)
        self.assertEqual([i['prompt'] for i in d], ['进行中'])

    def test_codex_new_prompt_retires_the_previous_turn(self):
        path = self.base/'rollout.jsonl'
        self.write(path, [
            {'type':'session_meta','payload':{'id':'t'}},
            {'type':'event_msg','timestamp':self.now-900,'payload':{'type':'task_started','turn_id':'a','started_at':self.now-900}},
            {'type':'event_msg','timestamp':self.now-890,'payload':{'type':'item_completed','turn_id':'a',
                'item':{'type':'UserMessage','content':[{'type':'text','text':'第一轮'}]}}},
            {'type':'event_msg','timestamp':self.now-800,'payload':{'type':'task_complete','turn_id':'a',
                'completed_at':self.now-800,'last_agent_message':'答'}},
            {'type':'event_msg','timestamp':self.now-100,'payload':{'type':'task_started','turn_id':'b','started_at':self.now-100}},
            {'type':'event_msg','timestamp':self.now-90,'payload':{'type':'item_completed','turn_id':'b',
                'item':{'type':'UserMessage','content':[{'type':'text','text':'新问题'}]}}},
        ])
        r,d,p,_ = c.Reader('chatgpt', self.base, 'rollout.jsonl', c.parse_codex).collect(self.now, True)
        self.assertEqual(([i['prompt'] for i in d], [i['prompt'] for i in r]), ([], ['新问题']))

    def test_remote_followups_are_cleaned(self):
        self.assertEqual(c.remote_followups({'followups':['一',3,'','二','三 <b>x</b>'],'followupCount':'9'}),
                         {'followups':['二','三 <b>x</b>'],'followupCount':9})
        self.assertEqual(c.remote_followups({'followups':'not a list'}),{})
        self.assertEqual(c.remote_followups({'followups':['一'],'followupCount':None})['followupCount'],1)

    def test_new_prompt_after_a_finished_turn_means_you_saw_it(self):
        # Same as on the servers: asking again in a finished conversation clears its finished turn.
        said=lambda t:{'type':'user/message','data':{'source':{'kind':'user'},'content':[{'type':'text','text':t}]}}
        reader=self.dsh([self.start(1,self.now-60),said('先修登录'),self.end(1,self.now-30),
                         self.start(2,self.now-10),said('再看下日志')])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(([i['prompt'] for i in r],d,p),(['再看下日志'],[],[]))
        # An error survives: asking again is no proof that you read the failure.
        reader=self.dsh([self.start(1,self.now-60),said('先修登录'),self.end(1,self.now-30,'error'),
                         self.start(2,self.now-10),said('再试一次')])
        p=reader.collect(self.now,True)[2]
        self.assertEqual([i['prompt'] for i in p],['先修登录'])
        self.assertEqual(p[0]['state'],'error')
        # Codex: a new turn with your message in the same thread.
        path=self.base/'rollout.jsonl'
        def event(kind, **args):return {'type':'event_msg','timestamp':self.now-2,'payload':dict(type=kind,**args)}
        ask=lambda tid,t:event('item_completed',turn_id=tid,item={'type':'UserMessage','content':[{'type':'text','text':t}]})
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
            event('task_started',turn_id='a',started_at=self.now-90),ask('a','先修登录'),
            event('task_complete',turn_id='a',completed_at=self.now-60,last_agent_message='好了'),
            event('task_started',turn_id='b',started_at=self.now-20),ask('b','再看下日志')])
        r,d,p,s=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)
        self.assertEqual(([i['prompt'] for i in r],d),(['再看下日志'],[]))
        # Once the new one finishes, only it is waiting for you.
        with path.open('a') as f:
            f.write(json.dumps(event('task_complete',turn_id='b',completed_at=self.now-1,last_agent_message='看完了'))+'\n')
        r,d,p,s=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)
        self.assertEqual([i['prompt'] for i in d],['再看下日志'])

    def test_claude_new_prompt_after_finish_clears_it_but_a_queued_one_does_not(self):
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300},
            {'type':'result','user_message_uuid':'a','subtype':'success','timestamp':self.now-250},
            {'type':'user','uuid':'b','timestamp':self.now-100,'message':{'content':'再看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-100}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(([i['prompt'] for i in r],d),(['再看下日志'],[]))
        # Typed while the first was still running (queued), so you had not seen its result yet: it stays.
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300},
            {'type':'user','uuid':'b','timestamp':self.now-280,'message':{'content':'再看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'queued','timestamp':self.now-280},
            {'type':'result','user_message_uuid':'a','subtype':'success','timestamp':self.now-250},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-240}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(([i['prompt'] for i in r],[i['prompt'] for i in d]),(['再看下日志'],['先修登录']))

    def test_claude_prompt_matches_command_uuid(self):
        path=self.base/'audit.jsonl'
        self.write(path,[{'type':'user','uuid':'u1','message':{'content':'请整理文件'},'timestamp':self.now-2},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-1},
            {'type':'result','user_message_uuid':'u1','subtype':'success','timestamp':self.now}])
        self.assertEqual(c.parse_claude(path)['turns']['u1']['prompt'],'请整理文件')

    def test_logs_rotate_during_continuous_run_and_repeated_errors_are_throttled(self):
        c.configure_logging(self.base)
        def cleanup():
            for h in c.LOGGER.handlers[:]:h.close();c.LOGGER.removeHandler(h)
            c._last_logs.clear()
        self.addCleanup(cleanup)
        c._last_logs.clear()
        with patch.object(c.time,'monotonic',return_value=1000):
            for _ in range(100):c.log('same bad source')
        self.assertEqual((self.base/'collector.log').read_text().count('same bad source'),1)
        for n in range(1500):c.LOGGER.warning('%s %s',n,'x'*1000)
        files=list(self.base.glob('collector.log*'))
        self.assertEqual(len(files),2)
        self.assertTrue(all(p.stat().st_size <= c.LOG_LIMIT for p in files))
        # Old oversized files are bounded at startup as well.
        cleanup()
        (self.base/'collector.log').write_bytes(b'x'*(c.LOG_LIMIT*3))
        c.configure_logging(self.base)
        self.assertTrue(all(p.stat().st_size <= c.LOG_LIMIT for p in self.base.glob('collector.log*')))

    def test_no_notification_store_is_accessed(self):
        readers=c.make_readers(self.base)
        import builtins
        real_open=builtins.open
        def checked_open(path,*args,**kwargs):
            self.assertNotIn('usernoted',str(path))
            self.assertNotIn('notificationcenter',str(path))
            return real_open(path,*args,**kwargs)
        with patch('builtins.open',side_effect=checked_open):
            result=c.collect_state(readers,self.now,{})
        self.assertEqual(result['accessMode'],'local-sessions')
        self.assertNotIn('notifications',result)

    def test_one_broken_source_does_not_hide_other_sources(self):
        reader=self.dsh([self.start(1,self.now-2),self.end(1,self.now)])
        bad=self.base/'work/session-bad/session.v4.jsonl.zstd'
        bad.parent.mkdir();bad.write_bytes(b'broken')
        state=c.collect_state([reader],self.now,{'deepseek':True})
        self.assertEqual(len(state['done']),1)
        self.assertEqual(state['agents']['deepseek']['status'],'partial')

    def test_private_state_and_secret_redaction(self):
        path=self.base/'state.json'
        c.write_atomic(path,'{}')
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertNotIn('sk-',c.one_line('test sk-abcdefghijklmnopqrst'))

    def test_empty_valid_source_is_connected(self):
        reader=c.Reader('deepseek',self.base,'*/session-*/session.v4.jsonl.zstd',c.parse_deepseek)
        self.assertEqual(reader.collect(self.now,True)[3],'ok')

    # --- stopped conversations must not stay "running" -------------------------

    def claude_audit(self, rows):
        path=self.base/'sess/abcd1234/audit.jsonl'
        self.write(path,rows)
        return c.Reader('claude',self.base,'*/*/audit.jsonl',c.parse_claude)

    def test_claude_interrupt_marker_ends_turn_and_is_not_a_prompt(self):
        reader=self.claude_audit([
            {'type':'user','uuid':'u1','timestamp':self.now-30,'message':{'content':'做一份周报'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-30},
            {'type':'assistant','timestamp':self.now-20},
            {'type':'user','uuid':'x','timestamp':self.now-5,'message':{'content':[{'type':'text','text':'[Request interrupted by user]'}]}}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(d),len(p)),(0,0,1))
        self.assertEqual((p[0]['state'],p[0]['prompt']),('interrupted','做一份周报'))

    def test_claude_finished_command_without_result_needs_confirmation_after_grace(self):
        reader=self.claude_audit([
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-60},
            {'type':'command_lifecycle','command_uuid':'u1','state':'completed','timestamp':self.now-5}])
        # Within the grace period a result row may still be on its way.
        self.assertEqual(len(reader.collect(self.now,True)[0]),1)
        r,d,p,s=reader.collect(self.now+c.RESULT_GRACE+1,True)
        self.assertEqual((len(r),len(d),len(p)),(0,0,1))
        self.assertEqual(p[0]['state'],'unknown')
        self.assertIn('未收到结果',p[0]['detail'])
        # A delayed result replaces the provisional state on the next poll.
        path=self.base/'sess/abcd1234/audit.jsonl'
        with path.open('a') as f:
            f.write(json.dumps({'type':'result','user_message_uuid':'u1','subtype':'success','timestamp':self.now+90})+'\n')
        r,d,p,s=reader.collect(self.now+91,True)
        self.assertEqual((len(r),len(d),len(p)),(0,1,0))

    def test_message_sent_while_claude_runs_is_part_of_that_task(self):
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-60,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-60},
            {'type':'user','uuid':'b','timestamp':self.now-50,'message':{'content':'顺便看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'queued','timestamp':self.now-50}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual([i['id'].split(':')[-1] for i in r],['a'])
        self.assertEqual((r[0]['prompt'],r[0]['followups']),('先修登录',['顺便看下日志']))
        self.assertEqual(len(p),0)

    def mid_task(self, result_for, follow_up_runs=True):
        rows=[{'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300},
            {'type':'user','uuid':'b','timestamp':self.now-200,'message':{'content':'顺便看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'queued','timestamp':self.now-200},
            {'type':'command_lifecycle','command_uuid':'a','state':'completed','timestamp':self.now-100}]
        if result_for == 'a':
            rows.append({'type':'result','user_message_uuid':'a','subtype':'success','result':'两件都好了','timestamp':self.now-99})
        if follow_up_runs:
            rows += [{'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-98},
                     {'type':'command_lifecycle','command_uuid':'b','state':'completed','timestamp':self.now-97}]
        if result_for == 'b':
            rows.append({'type':'result','user_message_uuid':'b','subtype':'success','result':'两件都好了','timestamp':self.now-96})
        return self.claude_audit(rows)

    def test_follow_up_answered_together_is_one_finished_task_not_unconfirmed(self):
        # Claude answers both messages in one go; whichever command carries the
        # result, the panel shows one finished task with your first prompt.
        for result_for in ('a','b'):
            for runs in (True, False):
                r,d,p,s=self.mid_task(result_for, runs).collect(self.now+c.RESULT_GRACE+1,True)
                label=(result_for, runs)
                self.assertEqual((len(r),len(d),len(p)),(0,1,0),label)
                self.assertEqual((d[0]['prompt'],d[0]['detail']),('先修登录','两件都好了'),label)
                self.assertEqual(d[0]['followups'],['顺便看下日志'],label)
                self.assertTrue(d[0]['id'].endswith(':a'),label)
                self.assertAlmostEqual(d[0]['start'],self.now-300,places=3)

    def test_message_sent_after_claude_finished_is_its_own_task(self):
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300},
            {'type':'result','user_message_uuid':'a','subtype':'success','timestamp':self.now-250},
            {'type':'user','uuid':'b','timestamp':self.now-100,'message':{'content':'再看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-100},
            {'type':'result','user_message_uuid':'b','subtype':'success','timestamp':self.now-10}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual([i['prompt'] for i in d],['再看下日志'])   # asked after the first finished: first is seen
        # Queued during the first run but executed alone with its own result: still separate.
        reader=self.claude_audit([
            {'type':'user','uuid':'a','timestamp':self.now-300,'message':{'content':'先修登录'}},
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-300},
            {'type':'user','uuid':'b','timestamp':self.now-280,'message':{'content':'再看下日志'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'queued','timestamp':self.now-280},
            {'type':'result','user_message_uuid':'a','subtype':'success','timestamp':self.now-250},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-240},
            {'type':'result','user_message_uuid':'b','subtype':'success','timestamp':self.now-10}])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(sorted(i['prompt'] for i in d),['先修登录','再看下日志'])

    def test_claude_completed_then_result_is_success(self):
        reader=self.claude_audit([
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-60},
            {'type':'command_lifecycle','command_uuid':'u1','state':'completed','timestamp':self.now-5},
            {'type':'result','user_message_uuid':'u1','subtype':'success','result':'好了','timestamp':self.now-4}])
        r,d,p,s=reader.collect(self.now+60,True)
        self.assertEqual((len(r),len(d),len(p)),(0,1,0))

    def test_claude_unfamiliar_stop_states_end_the_turn(self):
        for state,expected in (('stopped','interrupted'),('user_cancelled','interrupted'),('errored','error'),('killed','interrupted')):
            reader=self.claude_audit([
                {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-60},
                {'type':'command_lifecycle','command_uuid':'u1','state':state,'timestamp':self.now-1}])
            r,d,p,s=reader.collect(self.now,True)
            self.assertEqual((len(r),p[0]['state']),(0,expected),state)

    def test_claude_stop_does_not_hit_queued_command_behind_it(self):
        reader=self.claude_audit([
            {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-60},
            {'type':'command_lifecycle','command_uuid':'b','state':'queued','timestamp':self.now-50},
            {'type':'user','uuid':'x','timestamp':self.now-40,'message':{'content':'[Request interrupted by user]'}},
            {'type':'command_lifecycle','command_uuid':'b','state':'started','timestamp':self.now-30}])
        info=c.parse_claude(self.base/'sess/abcd1234/audit.jsonl')
        self.assertEqual(info['turns']['a']['state'],'interrupted')
        self.assertEqual(info['turns']['b']['state'],'running')
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(len(r),1)

    def test_claude_waiting_state_keeps_turn_running(self):
        reader=self.claude_audit([
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-300},
            {'type':'command_lifecycle','command_uuid':'u1','state':'waiting_for_permission','timestamp':self.now-200}])
        self.assertEqual(len(reader.collect(self.now,True)[0]),1)

    def test_codex_empty_error_is_success_and_dsh_odd_reason_does_not_crash(self):
        path=self.base/'rollout.jsonl'
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'r','started_at':self.now-9}},
            {'type':'event_msg','payload':{'type':'task_complete','turn_id':'r','completed_at':self.now-1,'error':{},'last_agent_message':'好了'}}])
        d=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)[1]
        self.assertEqual(d[0]['detail'],'好了')
        odd=self.dsh([self.start(1,self.now-9),{'type':'turn/end','time':self.now*1000,'data':{'turn':1,'reason':{'kind':'aborted','reason':'user'}}}])
        r,d,p,s=odd.collect(self.now,True)
        self.assertEqual((s,p[0]['state']),('ok','interrupted'))

    def test_prompt_is_the_users_first_question_of_the_turn(self):
        path=self.base/'rollout.jsonl'
        def event(kind, **args):return {'type':'event_msg','timestamp':self.now-2,'payload':dict(type=kind,**args)}
        def said(tid,text):return event('item_completed',turn_id=tid,item={'type':'UserMessage','content':[{'type':'text','text':text}]})
        self.write(path,[{'type':'session_meta','payload':{'id':'t'}},
            event('task_started',turn_id='a',started_at=self.now-90),
            said('a','启动页什么时候显示？'),
            said('a','**补充说明：**很长的粘贴内容'),          # mid-turn steering
            event('turn_aborted',turn_id='a',reason='interrupted'),
            event('task_started',turn_id='b',started_at=self.now-60,turn_attribution={'turn_trigger':'resume_interrupted_task'}),
            event('task_started',turn_id='c',started_at=self.now-30),
            said('c','<environment_context>x</environment_context>'),
            said('c','把按钮文字改短一点')])
        info=c.parse_codex(path)
        self.assertEqual(info['turns']['a']['prompt'],'启动页什么时候显示？')
        self.assertEqual(info['turns']['b']['prompt'],'启动页什么时候显示？')   # resumed: same question
        self.assertEqual(info['turns']['c']['prompt'],'把按钮文字改短一点')

    def test_prompt_preview_strips_app_wrappers_paths_and_escapes(self):
        self.assertEqual(c.prompt_preview('<system-reminder>\nmemory index\n</system-reminder>\n帮我看看桌宠'),'帮我看看桌宠')
        self.assertEqual(c.prompt_preview('/Users/someone/Documents/notes/meeting_draft.md 帮我改成 PPT'),'…/meeting_draft.md 帮我改成 PPT')
        self.assertEqual(c.prompt_preview('\\*\\*注意：\\*\\*会弹窗'),'注意：会弹窗')
        self.assertEqual(c.prompt_preview('看 /tmp/a.png'),'看 /tmp/a.png')     # short paths stay

    def test_stale_turn_logs_record_types_only(self):
        reader=self.claude_audit([
            {'type':'user','uuid':'u1','timestamp':self.now-4000,'message':{'content':'私密内容'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-4000}])
        with patch.object(c,'log') as logged:
            p=reader.collect(self.now+c.STALE+60,True)[2]
        self.assertEqual(p[0]['state'],'unknown')
        message=logged.call_args[0][0]
        self.assertIn('command_lifecycle:started',message)
        self.assertNotIn('私密内容',message)

    def test_codex_goal_is_shown_as_the_question(self):
        """Codex 的 goal 模式：目标写在 codex_internal_context 里，不是 UserMessage。

        真实现场：本机一个 goal 会话在跑，面板提问是空的。
        """
        path = self.base/'rollout.jsonl'
        def event(kind, **args):
            return {'type': 'event_msg', 'timestamp': self.now-2, 'payload': dict(type=kind, **args)}
        self.write(path, [
            {'type': 'session_meta', 'payload': {'id': 'thread'}},
            event('task_started', turn_id='g1', started_at=self.now-120),
            {'type': 'response_item', 'timestamp': self.now-119,
             'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text',
                 'text': '<codex_internal_context source="user_goal">\n'
                         'User set the goal: "把图片预览修好，先测试再让我装"\n'
                         'User set goal status: "active".\n'
                         '</codex_internal_context>'}]}},
        ])
        r, d, p, _ = c.Reader('chatgpt', self.base, 'rollout.jsonl', c.parse_codex).collect(self.now, True)
        self.assertEqual([i['prompt'] for i in r], ['把图片预览修好，先测试再让我装'])

    def test_codex_error_dict_shows_message_and_resumed_turn_is_marked(self):
        path=self.base/'rollout.jsonl'
        def event(kind, **args):return {'type':'event_msg','timestamp':self.now-2,'payload':dict(type=kind,**args)}
        self.write(path,[{'type':'session_meta','payload':{'id':'thread'}},
            event('task_started',turn_id='a',started_at=self.now-300),
            event('task_complete',turn_id='a',completed_at=self.now-250,error={'message':'exceeded retry limit, last status: 429'}),
            event('task_started',turn_id='b',started_at=self.now-200),
            event('item_completed',turn_id='b',item={'type':'UserMessage','content':[{'type':'text','text':'把配色换成浅色'}]}),
            event('turn_aborted',turn_id='b',reason='interrupted'),
            event('task_started',turn_id='c',started_at=self.now-10,turn_attribution={'turn_trigger':'resume_interrupted_task'})])
        r,d,p,s=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)
        # A failure is not cleared by later messages: only successes are retired that way.
        self.assertEqual([i['state'] for i in p],['error'])
        self.assertIn('429',p[0]['detail'])
        self.assertTrue(r[0]['resumed'])
        self.assertEqual(r[0]['prompt'],'把配色换成浅色')
        self.assertIn('自动继续',r[0]['detail'])
        # Codex resuming by itself is not you asking again: the error stays, with its message.
        self.write(path,[{'type':'session_meta','payload':{'id':'thread'}},
            event('task_started',turn_id='a',started_at=self.now-300),
            event('item_completed',turn_id='a',item={'type':'UserMessage','content':[{'type':'text','text':'跑一下'}]}),
            event('task_complete',turn_id='a',completed_at=self.now-250,error={'message':'exceeded retry limit, last status: 429'}),
            event('task_started',turn_id='c',started_at=self.now-10,turn_attribution={'turn_trigger':'resume_interrupted_task'})])
        r,d,p,s=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex).collect(self.now,True)
        self.assertEqual(p[0]['detail'],'exceeded retry limit, last status: 429')

    def test_claude_folder_access_shows_waiting_until_it_returns(self):
        """Claude 桌面版申请访问文件夹时，界面会弹权限框等你点。

        tool_use 已经写进记录、tool_result 还没回来，这一轮就是在等你批准，
        不能显示成"运行中"，否则你会以为它自己在跑。
        """
        path = self.base/'sess/abcd1234/audit.jsonl'
        rows = [
            {'type':'user','uuid':'u1','timestamp':self.now-120,
             'message':{'content':'帮我读一下这个文件夹'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-120},
            {'type':'assistant','uuid':'a1','timestamp':self.now-60,'message':{'content':[
                {'type':'tool_use','id':'toolu_ABC','name':'mcp__cowork__request_cowork_directory',
                 'input':{'path':'~/Desktop/Some.app/Contents/Resources'}}]}},
        ]
        self.write(path, rows)
        reader = c.Reader('claude', self.base, '*/*/audit.jsonl', c.parse_claude)
        r, d, p, _ = reader.collect(self.now, True)
        self.assertEqual([i['state'] for i in p], ['waiting'])
        self.assertIn('访问文件夹', p[0]['detail'])
        self.assertEqual(r, [])
        # 你点了允许、结果写回来之后，就不再是等待状态。
        rows.append({'type':'user','uuid':'u2','timestamp':self.now-10,'message':{'content':[
            {'type':'tool_result','tool_use_id':'toolu_ABC','content':'Folder connected'}]}})
        self.write(path, rows)
        r, d, p, _ = reader.collect(self.now, True)
        self.assertEqual(([i['prompt'] for i in r], p), (['帮我读一下这个文件夹'], []))

    def test_claude_file_delete_also_waits_for_approval(self):
        path = self.base/'sess/abcd1234/audit.jsonl'
        self.write(path, [
            {'type':'user','uuid':'u1','timestamp':self.now-120,'message':{'content':'清理旧图'}},
            {'type':'command_lifecycle','command_uuid':'u1','state':'started','timestamp':self.now-120},
            {'type':'assistant','uuid':'a1','timestamp':self.now-60,'message':{'content':[
                {'type':'tool_use','id':'toolu_DEL','name':'mcp__cowork__allow_cowork_file_delete',
                 'input':{'file_path':'/tmp/a/b.png'}}]}},
        ])
        p = c.Reader('claude', self.base, '*/*/audit.jsonl', c.parse_claude).collect(self.now, True)[2]
        self.assertEqual([i['state'] for i in p], ['waiting'])
        self.assertIn('删除文件', p[0]['detail'])

    def test_dsh_approval_request_shows_waiting_until_decided(self):
        ask={'type':'approval/asked','time':(self.now-30)*1000,'data':{'id':'a1','toolName':'Bash','callId':'c1','reason':'x'}}
        reader=self.dsh([self.start(1,self.now-60),ask])
        r,d,p,s=reader.collect(self.now+3600,True)          # waiting: never goes stale while you decide
        self.assertEqual((len(r),len(p)),(0,1))
        self.assertEqual((p[0]['state'],p[0]['detail']),('waiting','等你批准：Bash'))
        decided={'type':'approval/decided','time':(self.now-20)*1000,'data':{'id':'a1','outcome':'allowed-once'}}
        reader=self.dsh([self.start(1,self.now-60),ask,decided])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(p)),(1,0))              # approved: running again

    def test_dsh_shows_its_reply_like_the_other_agents(self):
        def said(step, *parts):
            return {'type':'assistant/message','time':(self.now-5)*1000,'data':{'turn':1,'step':step,
                    'message':{'role':'assistant','content':list(parts)}}}
        text=lambda t:{'type':'text','text':t}
        reader=self.dsh([self.start(1,self.now-60),
            said(1,{'type':'reasoning','text':'先想想'},{'type':'tool-call','name':'Bash'}),
            said(2,{'type':'reasoning','text':'看完了'},text('改好了：面板现在能看到回复。')),
            said(3,{'type':'tool-call','name':'Bash'}),          # a later step without text keeps the reply
            self.end(1,self.now-1)])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(d[0]['detail'],'改好了：面板现在能看到回复。')
        # Without any text the detail stays the plain "已完成" (the panel hides it).
        reader=self.dsh([self.start(1,self.now-9),said(1,{'type':'tool-call','name':'Bash'}),self.end(1,self.now-1)])
        self.assertEqual(reader.collect(self.now,True)[1][0]['detail'],'已完成')

    def test_claude_folder_access_request_waits_for_your_approval(self):
        start=[{'type':'user','uuid':'a','timestamp':self.now-60,'message':{'content':'整理下载文件夹'}},
               {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-60}]
        ask={'type':'assistant','timestamp':self.now-40,'message':{'content':[{'type':'tool_use','id':'toolu_d',
             'name':'mcp__cowork__request_cowork_directory','input':{'path':'~/Downloads'}}]}}
        answer={'type':'user','timestamp':self.now-10,'message':{'content':[{'type':'tool_result','tool_use_id':'toolu_d','content':'mounted'}]}}
        r,d,p,s=self.claude_audit(start+[ask]).collect(self.now+3600,True)   # never goes stale while the dialog is open
        self.assertEqual((len(r),[i['state'] for i in p],p[0]['detail'],p[0]['prompt']),
                         (0,['waiting'],'等你批准：访问文件夹 ~/Downloads','整理下载文件夹'))
        r,d,p,s=self.claude_audit(start+[ask,answer]).collect(self.now,True)
        self.assertEqual((len(r),len(p)),(1,0))                                # allowed: running again
        # Permission to delete files: same dialog-style wait. A long path is shortened.
        rm={'type':'assistant','timestamp':self.now-40,'message':{'content':[{'type':'tool_use','id':'toolu_r',
            'name':'mcp__cowork__allow_cowork_file_delete','input':{'file_path':'/Users/someone/Documents/old/report.docx'}}]}}
        p=self.claude_audit(start+[rm]).collect(self.now,True)[2]
        self.assertEqual(p[0]['detail'],'等你批准：删除文件 …/report.docx')
        # An ordinary MCP tool call is not a permission dialog.
        other={'type':'assistant','timestamp':self.now-40,'message':{'content':[{'type':'tool_use','id':'toolu_x',
               'name':'mcp__cowork__present_files','input':{}}]}}
        self.assertEqual(len(self.claude_audit(start+[other]).collect(self.now,True)[0]),1)

    def test_agent_question_waits_for_you_until_answered(self):
        q={'questions':[{'id':'q1','header':'方案','question':'用 A 还是 B？','options':[{'label':'A'},{'label':'B'}]},
                        {'id':'q2','header':'范围','question':'要不要顺便改测试？','options':[{'label':'要'}]}]}
        # DeepSeek Harness: ask_user_question is a tool call with no approval event.
        call={'type':'tool/call','time':(self.now-40)*1000,'data':{'turn':1,'step':3,'callId':'call_1','name':'ask_user_question','arguments':json.dumps(q)}}
        answer={'type':'tool/result','time':(self.now-10)*1000,'data':{'turn':1,'step':3,'message':{'role':'tool','toolCallId':'call_1','source':{'kind':'tool','callId':'call_1'},'content':[{'type':'text','text':'A'}]}}}
        reader=self.dsh([self.start(1,self.now-60),call])
        r,d,p,s=reader.collect(self.now+3600,True)          # never goes stale while you think
        self.assertEqual((len(r),len(p),p[0]['state']),(0,1,'question'))
        self.assertEqual(p[0]['detail'],'等你回答 2 个问题：用 A 还是 B？')
        reader=self.dsh([self.start(1,self.now-60),call,answer])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual((len(r),len(p)),(1,0))              # answered: running again
        # Claude: AskUserQuestion tool_use until its tool_result.
        ask={'type':'assistant','timestamp':self.now-40,'message':{'content':[{'type':'tool_use','id':'toolu_1','name':'AskUserQuestion','input':q}]}}
        reply={'type':'user','timestamp':self.now-10,'message':{'content':[{'type':'tool_result','tool_use_id':'toolu_1','content':'A'}]}}
        start=[{'type':'user','uuid':'a','timestamp':self.now-60,'message':{'content':'先修登录'}},
               {'type':'command_lifecycle','command_uuid':'a','state':'started','timestamp':self.now-60}]
        r,d,p,s=self.claude_audit(start+[ask]).collect(self.now,True)
        self.assertEqual((len(r),[i['state'] for i in p],p[0]['prompt']),(0,['question'],'先修登录'))
        r,d,p,s=self.claude_audit(start+[ask,reply]).collect(self.now,True)
        self.assertEqual((len(r),len(p)),(1,0))
        # ExitPlanMode is "confirm the plan"; an ordinary tool call is not a question.
        plan={'type':'assistant','timestamp':self.now-40,'message':{'content':[{'type':'tool_use','id':'toolu_2','name':'ExitPlanMode','input':{'plan':'x'}}]}}
        self.assertEqual(self.claude_audit(start+[plan]).collect(self.now,True)[2][0]['detail'],'等你确认计划')
        bash={'type':'assistant','timestamp':self.now-40,'message':{'content':[{'type':'tool_use','id':'toolu_3','name':'Bash','input':{}}]}}
        self.assertEqual(len(self.claude_audit(start+[bash]).collect(self.now,True)[0]),1)

    def test_codex_questions_wait_for_you(self):
        path=self.base/'rollout.jsonl'
        def event(kind, ago, **args):return {'type':'event_msg','timestamp':self.now-ago,'payload':dict(type=kind,**args)}
        def said(tid,text,ago):return event('item_completed',ago,turn_id=tid,item={'type':'UserMessage','content':[{'type':'text','text':text}]})
        def call(name,cid,ago,args):
            return {'type':'response_item','timestamp':self.now-ago,'payload':{'type':'function_call','name':name,'call_id':cid,
                    'arguments':json.dumps(args,ensure_ascii=False),'internal_chat_message_metadata_passthrough':{'turn_id':'a'}}}
        def output(cid,ago,out):return {'type':'response_item','timestamp':self.now-ago,'payload':{'type':'function_call_output','call_id':cid,'output':out}}
        reader=c.Reader('chatgpt',self.base,'rollout.jsonl',c.parse_codex)
        head=[{'type':'session_meta','payload':{'id':'t'}},event('task_started',90,turn_id='a',started_at=self.now-90),said('a','先修登录',89)]
        # Blocking request_user_input: waiting until its output, and never 待确认 meanwhile.
        ask=call('request_user_input','c1',80,{'questions':[{'id':'q1','header':'方案','question':'用 A 还是 B？','options':[{'label':'A','description':''}]}]})
        self.write(path,head+[ask])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(([i['state'] for i in p],p[0]['detail'],len(r)),(['question'],'等你回答：用 A 还是 B？',0))
        self.write(path,head+[ask,output('c1',20,'{"answers":{"q1":{"answers":["A"]}}}')])
        self.assertEqual(len(reader.collect(self.now,True)[0]),1)
        # Async: accepted at once, the turn ends, the question is still yours to answer.
        ask2=call('request_user_input_async','c2',80,{'questions':[{'title':'要不要顺便改测试？','options':['要','不要']}]})
        rows=head+[ask2,output('c2',79,'{"accepted":true}'),
                   event('task_complete',60,turn_id='a',completed_at=self.now-60,last_agent_message='请回答上面的问题')]
        self.write(path,rows)
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(([i['state'] for i in p],p[0]['detail'],d),(['question'],'等你回答：要不要顺便改测试？',[]))
        reply='<send_user_message_question_reply>\n[{"answer":"要","question":"要不要顺便改测试？","questionItemId":"x"}]\n</send_user_message_question_reply>'
        self.write(path,rows+[event('task_started',20,turn_id='b',started_at=self.now-20),said('b',reply,19)])
        r,d,p,s=reader.collect(self.now,True)
        self.assertEqual(([i['prompt'] for i in r],p,d),(['回答：要'],[],[]))

    def test_dsh_stop_and_error_details(self):
        stop=self.dsh([self.start(1,self.now-9),{'type':'turn/end','time':self.now*1000,'data':{'turn':1,'reason':{'kind':'aborted','reason':{'kind':'user'}}}}])
        self.assertEqual(stop.collect(self.now,True)[2][0]['state'],'interrupted')
        closed=self.dsh([self.start(1,self.now-9),{'type':'turn/end','time':self.now*1000,'data':{'turn':1,'reason':{'kind':'aborted','reason':{'kind':'disposed'}}}}])
        self.assertEqual(closed.collect(self.now,True)[2][0]['detail'],'应用关闭时中断')
        err=self.dsh([self.start(1,self.now-9),{'type':'turn/end','time':self.now*1000,'data':{'turn':1,'reason':{'kind':'error','error':{'message':'No available channel'}}}}])
        self.assertEqual(err.collect(self.now,True)[2][0]['detail'],'出错：No available channel')

    # --- DeepSeek：完成的轮次要带上助手的回复文本 -------------------------

    def dsh_reply(self, events, name='reply'):
        path = self.base/('work/session-%s/session.v4.jsonl.zstd' % name)
        rows = [{'type':'session','id':'session-%s' % name}] + events
        self.write(path, rows, True)
        return c.Reader('deepseek', self.base, '*/session-*/session.v4.jsonl.zstd', c.parse_deepseek)

    def say(self, turn, ts, text, step=1):
        return {'type':'assistant/message','time':ts*1000,
                'data':{'turn':turn,'step':step,'message':{'role':'assistant','content':[{'type':'text','text':text}]}}}

    def test_dsh_reply_text_becomes_the_detail(self):
        reader = self.dsh_reply([
            self.start(1, self.now-60),
            {'type':'user/message','time':(self.now-58)*1000,
             'data':{'source':{'kind':'user'},'content':[{'type':'text','text':'问一句'}]}},
            self.say(1, self.now-50, '第一步答案'),
            # 后续步骤常常只有推理或工具调用，不能把它们当成答案。
            {'type':'assistant/message','time':(self.now-45)*1000,
             'data':{'turn':1,'step':2,'message':{'role':'assistant','content':[{'type':'reasoning','text':'再想想'}]}}},
            self.say(1, self.now-40, '最终答案', step=3),
            self.end(1, self.now-30)])
        d = reader.collect(self.now, True)[1]
        self.assertEqual(d[0]['detail'], '最终答案')

    def test_dsh_reply_without_text_falls_back_to_plain_done(self):
        reader = self.dsh_reply([
            self.start(1, self.now-60), self.end(1, self.now-30)])
        self.assertEqual(reader.collect(self.now, True)[1][0]['detail'], '已完成')

    def test_dsh_error_detail_is_not_overwritten_by_a_reply(self):
        reader = self.dsh_reply([
            self.start(1, self.now-60),
            self.say(1, self.now-50, '我说到一半'),
            {'type':'turn/end','time':(self.now-30)*1000,
             'data':{'turn':1,'reason':{'kind':'error','error':{'message':'boom'}}}}])
        self.assertEqual(reader.collect(self.now, True)[2][0]['detail'], '出错：boom')

    def test_dsh_blocked_turn_explains_itself(self):
        reader = self.dsh_reply([
            self.start(1, self.now-60),
            self.say(1, self.now-50, '我说到一半'),
            {'type':'turn/end','time':(self.now-30)*1000,
             'data':{'turn':1,'reason':{'kind':'blocked'}}}])
        d = reader.collect(self.now, True)[1]
        self.assertEqual(d[0]['state'], 'success')
        self.assertIn('插件规则', d[0]['detail'])

    def test_dsh_reply_without_a_turn_id_is_not_attached_to_the_latest_turn(self):
        """缺少 turn 字段时必须丢弃，否则会挂到最后开启的那一轮上。"""
        reader = self.dsh_reply([
            self.start(1, self.now-200), self.end(1, self.now-190),
            self.start(2, self.now-100),
            {'type':'user/message','time':(self.now-98)*1000,
             'data':{'source':{'kind':'user'},'content':[{'type':'text','text':'第二轮'}]}},
            {'type':'assistant/message','time':(self.now-95)*1000,
             'data':{'step':1,'message':{'role':'assistant','content':[{'type':'text','text':'来路不明的回复'}]}}},
            self.end(2, self.now-90)])
        d = reader.collect(self.now, True)[1]
        self.assertEqual([i['detail'] for i in d], ['已完成'])

    def test_dsh_reply_is_redacted_and_capped(self):
        reader = self.dsh_reply([
            self.start(1, self.now-60),
            self.say(1, self.now-50, '用 sk-abcdefghijklmnopqrst 和 ' + 'x'*400),
            self.end(1, self.now-30)])
        detail = reader.collect(self.now, True)[1][0]['detail']
        self.assertNotIn('sk-abcdefghijklmnopqrst', detail)
        self.assertIn('[密钥已隐藏]', detail)
        self.assertLessEqual(len(detail), 120)


if __name__ == '__main__':unittest.main()

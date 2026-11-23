"""Exercise real A/B media and review with a local mock; no Go or paid models."""
import asyncio, importlib, importlib.util, io as buffer_io, json, os, shutil, subprocess, sys, tempfile, threading, time, types
from pathlib import Path
from http.server import ThreadingHTTPServer

PLUGIN=Path(__file__).resolve().parents[1]

def main():
    import psutil
    process = psutil.Process()
    existing_children = {child.pid for child in process.children(recursive=True)}
    root=Path(tempfile.mkdtemp(prefix='kk-runninghub-ab-'))
    os.environ['KKGO_DATA_DIR']=str(root/'results')
    sys.path.insert(0,os.environ.get('COMFYUI_PATH',str(PLUGIN.parents[1])))
    sys.argv=[sys.argv[0]]
    from comfy.cli_args import args as comfy_args
    comfy_args.cpu = True  # Media-only offline verification does not need CUDA.
    import comfy.model_management  # Finish core initialization before parallel worker threads.
    name='runninghub_ab_verification'
    package=types.ModuleType(name)
    package.__path__=[str(PLUGIN)]
    sys.modules[name]=package
    nodes=types.ModuleType(name+'.nodes')
    nodes.__path__=[str(PLUGIN/'nodes')]
    sys.modules[nodes.__name__]=nodes
    stages=importlib.import_module(name+'.nodes.kkgo_video_stages')
    backend=importlib.import_module(name+'.kkagent_bridge.python_backend')
    agent=importlib.import_module(name+".nodes.kkgo_video_agent")
    routing=importlib.import_module(name+'.kkagent_bridge.duration_routing')
    from comfy_api.latest import InputImpl
    from comfy_execution.graph_utils import ExecutionBlocker
    spec=importlib.util.spec_from_file_location('ab_local_model',Path(__file__).with_name('local_model.py'))
    mock=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    a_started,b_child_started=threading.Event(),threading.Event()
    calls,errors=[],[]
    lock=threading.Lock()
    class Handler(mock.ModelHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(raw)
            rules = body['messages'][0]['content']
            first_user = next(m for m in body['messages'] if m['role'] == 'user')
            task = first_user['content'][0]['text']
            chain = 'A' if rules == 'TEST_A' or '你是A链路最终审核AI' in rules else 'B'
            initial = not any(m['role'] in ['tool', 'assistant'] for m in body['messages'])
            tools = [t['function']['name'] for t in body.get('tools', [])]
            record = dict(chain=chain, stage=('segment' if rules == 'TEST_SEGMENT' else 'child' if rules == 'TEST_REVERSE'
                          else 'coordinator' if '你是B链路的视频反推主AI' in rules else 'review' if '你是B链路最终复核主AI' in rules
                          else 'a-review' if '你是A链路最终审核AI' in rules else 'A'),
                          initial=initial, started=time.time(), tools=tools,
                          user_messages=sum(m['role'] == 'user' for m in body['messages']))
            with lock:
                calls.append(record)
            try:
                assert ('AB_PROBE_A' if chain == 'A' else 'AB_PROBE_B') in task, 'wrong user request routed to chain'
                assert ('AB_PROBE_B' if chain == 'A' else 'AB_PROBE_A') not in task, 'cross-chain user history'
                assert record['user_messages'] == 1, 'unexpected user history'
                has_cut = 'mcp__video__set_video_segments' in tools
                assert has_cut == (rules == 'TEST_SEGMENT'), 'segmentation tool leaked across stages'
                if rules == 'TEST_A' and initial:
                    a_started.set()
                    assert b_child_started.wait(60), 'B could not progress while A request was active'
                if rules == 'TEST_REVERSE' and initial:
                    assert a_started.is_set(), 'A/B overlap was not exercised'
                    b_child_started.set()
                if record['stage'] == 'a-review':
                    assert len(body['messages']) == 2, 'A review reused the draft or previous task history'
                    response = {'choices': [{'message': {'role': 'assistant', 'content': json.dumps({
                        'status': 'corrected', 'checks': {'A审核': True}, 'issues': [],
                        'edits': [{'segment_index': 1, 'old': '保留原规则和工具素材',
                                   'new': '审核后保留原规则和工具素材', 'reason': 'local patch probe'}]}, ensure_ascii=False)},
                        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}
                    self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode())
                    return
                self.rfile = buffer_io.BytesIO(raw)
                super().do_POST()
            except BaseException as exc:
                with lock:
                    errors.append(type(exc).__name__ + ': ' + str(exc))
                raise
            finally:
                record['ended'] = time.time()

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    # Route the fixed provider to the local fake only inside this verification process.
    agent.REVERSE_SETTINGS.update(base_url=f"http://127.0.0.1:{server.server_port}/v1", model="local-model")
    agent.REVIEW_SETTINGS.update(base_url=f"http://127.0.0.1:{server.server_port}/v1", model="local-model")
    report={'remote_model_requests':0,'plugin':str(PLUGIN),'root':str(root),'checks':{}}
    try:
        ffmpeg = shutil.which('ffmpeg')
        a_file, b_file = root / 'A.mp4', root / 'B.mp4'
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=green:s=128x128:r=10:d=3', '-c:v', 'libx264', '-threads', '1', str(a_file)], check=True)
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=red:s=128x128:r=10:d=16', '-f', 'lavfi', '-i',
                        'color=c=blue:s=128x128:r=10:d=17', '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
                        '-map', '[v]', '-c:v', 'libx264', '-threads', '1', str(b_file)], check=True)

        class ANode(stages.KKGO_VideoSegments):
            hidden = types.SimpleNamespace(unique_id='AB-check-A')

        class BNode(stages.KKGO_VideoSegments):
            hidden = types.SimpleNamespace(unique_id='AB-check-B')

        for duration, expected in [(31.9, 'A'), (31.999, 'A'), (32, 'B'), (60, 'B')]:
            assert routing.route_duration(duration)['chain'] == expected
        for duration in [0, float('nan'), 60.001]:
            try:
                routing.route_duration(duration)
                raise AssertionError('invalid duration accepted')
            except ValueError:
                pass
        report['checks']['duration_boundaries_mutually_exclusive'] = True
        small, large = InputImpl.VideoFromFile(str(a_file)), InputImpl.VideoFromFile(str(b_file))
        assert ANode.check_lazy_status(small, rules_text='TEST_A', segment_rules_text=None,
                                      chain_b_reverse_rules_text=None, chain_b_review_rules_text=None) == []
        assert BNode.check_lazy_status(large, rules_text=None, segment_rules_text='TEST_SEGMENT',
                                      chain_b_reverse_rules_text='TEST_REVERSE', chain_b_review_rules_text=mock.json.dumps({})) == []
        report['checks']['only_selected_chain_rule_fields_requested'] = True
        common = dict(api_key='offline-placeholder', review_api_key='review-placeholder', base_url=f'http://127.0.0.1:{server.server_port}/v1',
                      model='local-model', api_protocol='openai-completions', timeout_s=120,
                      reasoning_effort='high', stream_usage=True, keep_audio=False)

        async def run_chain(chain):
            if chain == 'A':
                entry = await ANode.execute(small, requirement='AB_PROBE_A', rules_text='TEST_A',
                                            segment_rules_text=None, chain_b_reverse_rules_text=None,
                                            chain_b_review_rules_text=None, **common)
                assert entry[3] is small and all(isinstance(entry[i], ExecutionBlocker) for i in range(4, 8))
            else:
                entry = await BNode.execute(large, requirement='AB_PROBE_B', rules_text=None,
                                            segment_rules_text='TEST_SEGMENT', chain_b_reverse_rules_text='TEST_REVERSE',
                                            chain_a_review_rules_text=None, **common)
                assert not isinstance(entry[3], ExecutionBlocker) and not isinstance(entry[4], ExecutionBlocker)
                assert all(isinstance(entry[i], ExecutionBlocker) for i in range(5, 8))
            # A job consumed twice must finish once, then return the cached result.
            job = entry[8]
            first, repeated = await asyncio.gather(asyncio.to_thread(job.finish), asyncio.to_thread(job.finish))
            assert first == repeated, 'same VideoJob produced different results'
            evidence = json.loads(first[1])
            assert evidence['chain'] == chain and evidence['chain_status'] == 'completed'
            if chain == 'A':
                assert evidence['video_outputs']['count'] == 1
                assert evidence['output_compliance']['review_turns'] == 1
                assert '审核后保留原规则和工具素材' in first[0]
                assert evidence['segment_agent_tool_calls'] == []
            else:
                assert evidence['video_outputs']['count'] == 2
                assert evidence['parallel_reverse']['review']['status'] == 'corrected'
            return dict(chain=chain, text=first[0], run_id=evidence['token_speed']['run_id'])

        async def together():
            return await asyncio.gather(run_chain('A'), run_chain('B'))

        report['results'] = asyncio.run(together())
        assert not errors, errors
        assert report['results'][0]['run_id'] != report['results'][1]['run_id']
        a_intervals = [c for c in calls if c['chain'] == 'A']
        child_intervals = [c for c in calls if c['stage'] == 'child']
        assert any(a['started'] < b['ended'] and b['started'] < a['ended'] for a in a_intervals for b in child_intervals)
        assert len([c for c in calls if c['stage'] == 'A']) == 2, 'VideoJob duplicate executed A twice'
        assert len([c for c in calls if c['stage'] == 'a-review']) == 1, 'VideoJob repeated A review'
        assert len([c for c in calls if c['stage'] == 'review']) == 1, 'VideoJob duplicate executed B twice'
        report['checks'].update(concurrent_A_B_model_requests_overlap=True,
                                no_cross_chain_user_or_tool_context=True,
                                A_passthrough_and_B_split_video_outputs=True,
                                A_ignores_B_rules_and_uses_shared_reviewer=True,
                                B_ignores_A_rules=True, B_children_and_reviewer_completed=True,
                                VideoJob_double_consumption_idempotent=True,
                                A_cleanup_does_not_cancel_B_task=True,
                                monitoring_run_ids_separate=True)

        report['checks']['all_model_connections_and_workers_closed'] = backend.runtime_status()['active_turns'] == 0 and backend.runtime_status()['media_workers'] == 0
        assert report['checks']['all_model_connections_and_workers_closed'], backend.runtime_status()
        time.sleep(.2)
        remaining = [child.pid for child in process.children(recursive=True) if child.pid not in existing_children and child.is_running()]
        assert not remaining, remaining
        report['checks']['no_remaining_owned_subprocesses'] = True
        report['success']=True
    except BaseException as exc:
        report['success']=False
        report['error']=type(exc).__name__+': '+str(exc)
        raise
    finally:
        server.shutdown()
        server.server_close()
        report['requests']=calls
        report['errors']=errors
        report['runtime_after']=backend.runtime_status()
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(root/'report.json'),'success':report.get('success'),'error':report.get('error'),'checks':report['checks']},ensure_ascii=False),flush=True)

if __name__=='__main__':
    main()

import importlib.util
import json
import time
from pathlib import Path
from serverbot.collectors.host import read_host_snapshot


def test_host_snapshot_fresh(tmp_path):
    path=tmp_path/'host.json'
    path.write_text(json.dumps({'timestamp':time.time(),'checks':[
        dict(key='bot:chatbot',name='ChatBot',status='ok',detail='one process')]}))
    assert read_host_snapshot(path)[0].status == 'ok'


def test_host_snapshot_rejects_stale_missing_and_bad_payload(tmp_path):
    path=tmp_path/'host.json'
    assert read_host_snapshot(path)[0].status == 'error'
    for payload in ({'timestamp':time.time()-1000,'checks':[]},
                    {'timestamp':time.time()+1000,'checks':[]},
                    {'timestamp':time.time(),'checks':[None]},
                    {'timestamp':time.time(),'checks':[]},
                    {'timestamp':time.time(),'checks':[dict(key='x',name='x',status='ok',detail=1)]}):
        path.write_text(json.dumps(payload))
        assert read_host_snapshot(path)[0].status == 'error'
    path.write_bytes(b'x'*131073)
    assert read_host_snapshot(path)[0].status == 'error'


def test_host_snapshot_rejects_duplicate_checks(tmp_path):
    path=tmp_path/'host.json'
    item=dict(key='x',name='x',status='ok',detail='ok')
    path.write_text(json.dumps(dict(timestamp=time.time(),checks=[item,item])))
    assert read_host_snapshot(path)[0].status == 'error'


def test_apk_plan_parser_detects_protected_dependency():
    import sys
    host=Path(__file__).resolve().parents[2]/'deploy/host'
    sys.path.insert(0,str(host))
    try:
        import updates
        packages=updates.planned_packages('( 1/2) Upgrading curl (1 -> 2)\n( 2/2) Upgrading musl (1 -> 2)\n')
        assert packages == ['curl','musl']
        assert any(p not in updates.ALLOW for p in packages)
    finally:
        sys.path.remove(str(host))


def test_updates_never_commits_protected_transaction(tmp_path, monkeypatch):
    import sys
    import subprocess
    host=Path(__file__).resolve().parents[2]/'deploy/host'
    sys.path.insert(0,str(host))
    try:
        import updates
        monkeypatch.setattr(updates,'BASE',tmp_path)
        original=Path.read_text
        monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw: 'https://example.org/alpine/v3.24/main' if str(path)=='/etc/apk/repositories' else original(path,*a,**kw))
        calls=[]
        def execute(args,timeout=180):
            calls.append(args)
            output=''
            if args[1:] == ['info']:
                output='curl\n'
            elif '--simulate' in args:
                output='( 1/2) Upgrading curl (1 -> 2)\n( 2/2) Upgrading musl (1 -> 2)\n'
            elif args[1:]==['version','-l','<']:
                output='curl-1-r0 < 2-r0\n'
            return subprocess.CompletedProcess(args,0,output,'')
        monkeypatch.setattr(updates,'execute',execute)
        updates.main()
        assert not any(args[1]=='upgrade' and '--simulate' not in args for args in calls)
        assert 'protected dependencies' in json.loads((tmp_path/'updates.json').read_text())['detail']
    finally:
        sys.path.remove(str(host))


def test_probe_drops_supplementary_groups_for_bot_healthcheck(monkeypatch):
    import sys
    import subprocess
    host=Path(__file__).resolve().parents[2]/'deploy/host'
    sys.path.insert(0,str(host))
    try:
        import probe
        recorded={}
        def fake_run(args,**kwargs):
            recorded.update(kwargs)
            return subprocess.CompletedProcess(args,0,'ok','')
        monkeypatch.setattr(probe.subprocess,'run',fake_run)
        assert probe.run(['/bin/true'],user='chatbot') == (0,'ok')
        assert recorded['user'] == 'chatbot'
        assert recorded['group'] == 'chatbot'
        assert recorded['extra_groups'] == ()
    finally:
        sys.path.remove(str(host))

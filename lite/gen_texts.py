#!/usr/bin/env python3
"""Generate lite/texts.ps1, the static-text section of harness-lite.ps1.

Everything default `harness host` sends upstream, byte-exact:
  - opencode's own texts (system prompts per model family, tool definitions,
    the built-in skill) from lite/opencode-texts.json, captured once per
    opencode version;
  - the proxy's texts (reminder.md, tool-guidance.json, corrections, slim
    prompts, slim tool descriptions) rendered by the real proxy/proxy.py.
No lite-specific edits. A proxy prompt change therefore changes texts.ps1;
tests/unit_lite_test.sh fails until `python3 lite/build.py` is rerun and the
result committed.

Usage: python3 lite/gen_texts.py [--out FILE] [--proxy-dir DIR]"""
import argparse, ast, copy, json, os, re, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))


def load_proxy(proxy_dir):
    # Tracked defaults only: INSTALL_ROOT would point reminder.md and
    # tool-guidance.json at the user's edited copies, and the PROXY_/HARNESS_
    # switches change what the proxy renders.
    for k in list(os.environ):
        if k == 'INSTALL_ROOT' or k.startswith(('PROXY_', 'HARNESS_', 'OAI_', 'CHATGPT_')):
            del os.environ[k]
    # CI's unit job has no pip installs; the proxy's web layer is not used here
    # (same stubs as tests/unit_mock_upstream_test.sh).
    try:
        import flask  # noqa: F401
    except ImportError:
        class _App:
            def __init__(self, *a, **k): pass
            def __getattr__(self, name): return lambda *a, **k: (lambda f: f)
        class _G:
            def get(self, name, default=None): return getattr(self, name, default)
        sys.modules['flask'] = types.SimpleNamespace(Flask=_App, Response=object, g=_G(), request=None)
    try:
        import requests  # noqa: F401
    except ImportError:
        class _RequestException(Exception): pass
        _u3 = types.SimpleNamespace(disable_warnings=lambda *a, **k: None,
                                    exceptions=types.SimpleNamespace(InsecureRequestWarning=Warning))
        sys.modules['requests'] = types.SimpleNamespace(
            RequestException=_RequestException, post=None,
            packages=types.SimpleNamespace(urllib3=_u3))
    sys.path.insert(0, proxy_dir)
    import proxy
    proxy._RUN_MODE = 'host'
    proxy._HOST_OS = 'linux'
    proxy._REQUIRE_TOOL_ENABLED = True
    return proxy


def generate(proxy_dir):
    proxy = load_proxy(proxy_dir)
    src = open(os.path.join(proxy_dir, 'proxy.py'), encoding='utf-8').read()
    C = {}   # module-level literal constants, read without executing anything
    for node in ast.parse(src).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            ts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in ts:
                if isinstance(t, ast.Name):
                    try: C[t.id] = ast.literal_eval(node.value)
                    except Exception: pass

    oc = json.load(open(os.path.join(HERE, 'opencode-texts.json'), encoding='utf-8'))
    assert oc['env_mark'] == proxy._ENV_MARK, 'proxy._ENV_MARK changed: recapture opencode-texts.json'
    P, defs, SETS = oc['prompts'], oc['tool_defs'], oc['tool_sets']

    T = {}
    for key, names in SETS.items():
        tools = [defs[n] for n in names]
        pt = proxy._augment_tools_with_finish(tools)
        tt = proxy.format_tools_to_text(pt)
        out = proxy.translate_history_and_apply_prompt(
            [{'role': 'system', 'content': 'SYSX'}, {'role': 'user', 'content': 'UQ'}], tt, pt, session_id='x', mode='hybrid')
        c0 = out[0]['content']
        pre = '<<<BEGIN_AGENT_INSTRUCTIONS>>>\nSYSX\n<<<END_AGENT_INSTRUCTIONS>>>\n'
        assert c0.startswith(pre), c0[:80]
        sa = c0[len(pre):]
        sa = sa[:sa.rindex('\n\nSession: ')]
        T['SA_' + key] = sa
        sig = proxy._extract_tool_signatures(pt, tt)
        det = proxy._extract_tool_details(pt, proxy._HYBRID_DETAIL_TOOLS)
        T['TE_' + key] = proxy._format_tool_entries(sig, det)
        # native (slim) tools, as opencode sends them after the proxy's _slim_tools
        T['NT_' + key] = json.dumps(proxy._slim_tools(copy.deepcopy(tools)), ensure_ascii=False)
        T['NAMES_' + key] = ' '.join(names)
    # required params per tool (for the argument check)
    T['ToolRequired'] = json.dumps({n: (t['function']['parameters'].get('required') or []) for n, t in defs.items()})

    # reminder template, exactly as the proxy loads it
    proxy._REMINDER_TEMPLATE = None
    proxy._setup_reminder_template()
    rem = proxy._REMINDER_TEMPLATE
    assert rem != proxy._REMINDER_FALLBACK, 'proxy/reminder.md unreadable'
    rem = '\n'.join(l for l in rem.split('\n') if not proxy._REMINDER_INSTRUCTIONS_LINE_RE.fullmatch(l))
    T['Reminder'] = rem
    T['Correction'] = C['_REQUIRE_TOOL_CORRECTION']
    T['Escalation'] = C['_REQUIRE_TOOL_ESCALATION']
    T['FinishTodos'] = C['_REQUIRE_TOOL_FINISH_TODOS_CORRECTION']
    T['EmptyFinish'] = C['_REQUIRE_TOOL_EMPTY_FINISH_TEXT']
    T['RetryEscape'] = C['_RETRY_CORRECTION_MESSAGES']['malformed_escape']
    T['RetryFence'] = C['_RETRY_CORRECTION_MESSAGES']['malformed_fence']
    T['GitBashNote'] = C['_GIT_BASH_NOTE']
    T['SlimCapNote'] = proxy._SLIM_CAP_NOTE
    T['SlimMain'] = proxy._slim_prompt('main')
    T['SlimGeneral'] = proxy._slim_prompt('general')
    assert T['SlimMain'] and T['SlimGeneral']

    env_src = re.search(r'if _RUN_MODE == "host":\n\s*return \((.*?)\)\n', src, re.S).group(1)
    env_pieces = [ast.literal_eval(x) for x in re.findall(r'("(?:[^"\\]|\\.)*")', env_src)]
    T['EnvHead'] = env_pieces[0]
    T['EnvTail'] = "".join(x for x in env_pieces[1:] if x != "windows")
    assert T['EnvHead'] == "you run directly on the user's own machine"
    # check the environment clause against the proxy's own
    assert proxy._format_environment_clause() == T['EnvHead'] + ' (host OS: linux)' + T['EnvTail'], proxy._format_environment_clause()
    T['NoTodos'] = proxy._format_todos_block([])
    T['TodosHead'] = proxy._format_todos_block([{'content': 'ZZ', 'status': 'pending'}]).split('ZZ')[0]
    T['TodosHead'] = T['TodosHead'][:T['TodosHead'].rindex('\n') + 1]
    T['InstrClause'] = proxy._instructions_clause(['@@A@@', '@@B@@'])

    T['SkillBuiltin'] = oc['skill_builtin']
    T['SkillBuiltinDesc'] = oc['skill_builtin_desc']
    for k, v in P.items(): T['P_' + k] = v

    codes = set()
    def enc(s):
        out = []
        for ch in s:
            if ord(ch) > 127:
                codes.add(ord(ch)); out.append('<%%%04X%%>' % ord(ch))
            else: out.append(ch)
        s = ''.join(out)
        assert "\n'@" not in s and not s.startswith("'@")
        return s
    body = ["# ---------------------------------------------------------------- texts",
            "# Generated by gen_texts.py from the harness sources (proxy/proxy.py,",
            "# proxy/reminder.md, proxy/tool-guidance.json, agents/*.md) and opencode",
            "# 1.19.3's own prompts and tool definitions. Non-ASCII characters are",
            "# written as <%XXXX%> so this file stays plain ASCII.",
            "$script:TX = @{}"]
    for k, v in T.items():
        body.append(f"$script:TX['{k}'] = T @'\n{enc(v)}\n'@")
    codes_ps = ', '.join('0x%04X' % c for c in sorted(codes))
    hd = f"""function T([string]$s) {{
    foreach ($c in @({codes_ps})) {{ $s = $s.Replace(('<%' + ('{{0:X4}}' -f $c) + '%>'), [string][char]$c) }}
    return $s.Replace("`r`n", "`n")
}}"""
    return hd + "\n" + "\n".join(body) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default=os.path.join(HERE, 'texts.ps1'))
    ap.add_argument('--proxy-dir', default=os.path.join(os.path.dirname(HERE), 'proxy'))
    a = ap.parse_args()
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):   # the proxy prints banners on import
        text = generate(os.path.abspath(a.proxy_dir))
    with open(a.out, 'w', encoding='ascii', newline='\n') as f:
        f.write(text)
    print(f'wrote {a.out} ({len(text)} bytes)')


if __name__ == '__main__':
    main()

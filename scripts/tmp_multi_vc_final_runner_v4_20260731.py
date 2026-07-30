from pathlib import Path

V3 = Path(__file__).with_name("tmp_multi_vc_final_runner_v3_20260731.py")
source = V3.read_text(encoding="utf-8")
source = source.replace(
    'namespace: dict[str, object] = {}',
    'namespace: dict[str, object] = {"__file__": str(BASE_RUNNER), "__name__": "__runner_prefix__"}',
    1,
)
exec(compile(source, str(V3), "exec"), {"__file__": str(V3), "__name__": "__main__"})

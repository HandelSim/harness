import json, sys
WANT = {"hardware": 3496, "software": 2089, "travel": 1078, "training": 1705, "total": 8368}
try:
    got = json.load(open("summary.json"))
except Exception:
    print("summary.json missing or not JSON"); sys.exit(1)
if not isinstance(got, dict) or {k: got.get(k) for k in WANT} != WANT:
    bad = [k for k in WANT if not isinstance(got, dict) or got.get(k) != WANT[k]]
    print("wrong values for: %s" % ",".join(bad)); sys.exit(1)
print("ok")

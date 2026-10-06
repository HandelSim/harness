import sys
try:
    lines = [l.strip() for l in open("report.txt").read().strip().splitlines() if l.strip()]
except OSError:
    print("report.txt missing"); sys.exit(1)
want = ["code=KX-4417-QV", "blockers=3"]
if lines != want:
    print("report.txt has %d line(s), code %s, blockers %s" % (
        len(lines), "ok" if want[0] in lines else "wrong", "ok" if want[1] in lines else "wrong"))
    sys.exit(1)
print("ok")

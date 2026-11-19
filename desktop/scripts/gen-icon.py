# Regenerates build/icon.png: a Lorenz attractor (the app is about chaos tamed). Deterministic.
# Usage: python3 scripts/gen-icon.py > /tmp/icon.svg && rsvg-convert -w 1024 -h 1024 /tmp/icon.svg -o build/icon.png
import numpy as np, sys
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
s, r, b = 10.0, 28.0, 8/3
def f(p): x,y,z=p; return np.array([s*(y-x), x*(r-z)-y, x*y-b*z])
p = np.array([1.0, 1.0, 20.0]); dt = 0.008
for _ in range(3000):  # transient (RK4)
    k1=f(p);k2=f(p+dt/2*k1);k3=f(p+dt/2*k2);k4=f(p+dt*k3);p=p+dt/6*(k1+2*k2+2*k3+k4)
pts=[]
for _ in range(N):
    k1=f(p);k2=f(p+dt/2*k1);k3=f(p+dt/2*k2);k4=f(p+dt*k3);p=p+dt/6*(k1+2*k2+2*k3+k4); pts.append(p.copy())
P=np.array(pts)
# project: slight rotation for depth, x horizontal, z vertical
th=0.35; X=P[:,0]*np.cos(th)+P[:,1]*np.sin(th); Z=P[:,2]
S=1024; inset=64; box=S-2*inset
cx=(X.max()+X.min())/2; cz=(Z.max()+Z.min())/2; span=max(X.max()-X.min(), (Z.max()-Z.min()))
k=box*0.74/span
u=S/2+(X-cx)*k; v=S/2-(Z-cz)*k+10
stops=[(0,(94,234,212)),(0.5,(129,140,248)),(1,(244,114,182))]
def col(t):
    for (a,ca),(bb,cb) in zip(stops,stops[1:]):
        if t<=bb: w=(t-a)/(bb-a); return '#%02x%02x%02x'%tuple(int(ca[i]+(cb[i]-ca[i])*w) for i in range(3))
    return '#%02x%02x%02x'%stops[-1][1]
xn=(X-X.min())/(X.max()-X.min())
seg=12; paths=[]
for i in range(0,N-1,seg):
    j=min(i+seg+1,N); t=float(xn[i:j].mean())
    d='M'+' L'.join('%.1f %.1f'%(u[q],v[q]) for q in range(i,j))
    paths.append((d,col(t)))
def layer(w,op): return '\n'.join(f'<path d="{d}" stroke="{c}" stroke-width="{w}" stroke-opacity="{op}"/>' for d,c in paths)
svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" viewBox="0 0 {S} {S}">
<defs>
 <radialGradient id="bg" cx="50%" cy="42%" r="75%"><stop offset="0" stop-color="#1e1b4b"/><stop offset="0.6" stop-color="#0f1029"/><stop offset="1" stop-color="#070814"/></radialGradient>
 <linearGradient id="rim" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#a5b4fc" stop-opacity="0.55"/><stop offset="1" stop-color="#a5b4fc" stop-opacity="0.08"/></linearGradient>
 <filter id="glow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="18"/></filter>
 <clipPath id="clip"><rect x="{inset}" y="{inset}" width="{box}" height="{box}" rx="210"/></clipPath>
</defs>
<rect x="{inset}" y="{inset}" width="{box}" height="{box}" rx="210" fill="url(#bg)"/>
<g clip-path="url(#clip)" fill="none" stroke-linecap="round" stroke-linejoin="round">
 <g filter="url(#glow)">{layer(34,0.55)}</g>
 <g>{layer(13,0.95)}</g>
 <g>{layer(4,0.9).replace('stroke="#','stroke="#ffffff" data-c="#')}</g>
</g>
<rect x="{inset+3}" y="{inset+3}" width="{box-6}" height="{box-6}" rx="207" fill="none" stroke="url(#rim)" stroke-width="6"/>
</svg>'''
sys.stdout.write(svg)

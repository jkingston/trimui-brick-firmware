#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import hashlib,json,pathlib
P=pathlib.Path(__file__).resolve().parent
D=P/'fixtures'/'g2d';D.mkdir(parents=True,exist_ok=True)
for p in D.iterdir():
 if p.suffix in ('.input','.expected','.mask'):p.unlink()
def layout(fmt,w,h):
 s=((w*(3 if fmt=='RGB3' else 1)+ (15 if fmt=='RGB3' else 31))//(16 if fmt=='RGB3' else 32))*(16 if fmt=='RGB3' else 32)
 return s,s*h if fmt=='RGB3' else s*h*3//2
def image(fmt,w,h,pixel,masked=lambda x,y:True):
 s,n=layout(fmt,w,h);b=bytearray([165]*n);mask=bytearray(n)
 for y in range(h):
  for x in range(w):
   p=pixel(x,y)
   for c in range(3):
    if fmt=='RGB3': i=y*s+x*3+c
    elif c==0:i=y*s+x
    else:
     if x%2 or y%2:continue
     i=s*h+(c-1)*s*h//4+(y//2)*(s//2)+x//2
    b[i]=p[c];mask[i]=int(masked(x,y))
 return b,mask
cases=[]
def add(name,inf,outf,w,h,ow,oh,pix,expected,rot=0,hf=0,vf=0,enc=1,tolerance=0,masked=lambda x,y:True,note=''):
 b,_=image(inf,w,h,pix);e,m=image(outf,ow,oh,expected,masked)
 for ext,data in [('input',b),('expected',e),('mask',m)]: (D/(name+'.'+ext)).write_bytes(data)
 cases.append(dict(name=name,args=[inf,outf,w,h,ow,oh,rot,hf,vf,enc],tolerance=tolerance,note=note,checked_bytes=sum(m),input_sha256=hashlib.sha256(b).hexdigest(),expected_sha256=hashlib.sha256(e).hexdigest(),mask_sha256=hashlib.sha256(m).hexdigest()))
def pattern(x,y):return ((x*17+y*3)%256,(x*5+y*23)%256,(x*11+y*7)%256)
w,h=34,18
for rot in (0,90,180,270):
 ow,oh=(h,w) if rot%180 else (w,h)
 def exp(x,y,r=rot):
  sx,sy={0:(x,y),90:(y,h-1-x),180:(w-1-x,h-1-y),270:(w-1-y,x)}[r]
  return pattern(sx,sy)
 add('rotate-'+str(rot),'RGB3','RGB3',w,h,ow,oh,pattern,exp,rot=rot,note='90/270 use clockwise convention; opposite orientation is reported as mismatch, requires contract review.')
for flip in ('h','v'):
 add('flip-'+flip,'RGB3','RGB3',w,h,w,h,pattern,lambda x,y,f=flip:pattern(w-1-x if f=='h' else x,h-1-y if f=='v' else y),hf=int(flip=='h'),vf=int(flip=='v'))
(P/'manifest.json').write_text(json.dumps(dict(cases=cases),indent=2)+'\n')
(P/'cases.tsv').write_text(''.join(
    c['name'] + '\t' + '\t'.join(map(str, c['args'])) + '\t' + str(c['tolerance']) + '\n'
    for c in cases))
print(len(cases),'cases generated')

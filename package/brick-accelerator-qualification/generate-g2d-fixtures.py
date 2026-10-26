#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import hashlib,json,pathlib
P=pathlib.Path(__file__).resolve().parent
D=P/'fixtures'/'g2d';D.mkdir(parents=True,exist_ok=True)
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
# Exact fixed-point matrices programmed by source-supported G2D driver.
mat={
 (1,'RGB3'):[[263,516,100,16896],[-152,-298,450,131584],[450,-377,-73,131584]],
 (2,'RGB3'):[[187,629,63,16896],[-103,-346,450,131584],[450,-409,-41,131584]],
 (1,'YU12'):[[1192,0,1634,-227750],[1192,-400,-833,139252],[1192,2066,0,-283062]],
 (2,'YU12'):[[1192,0,1836,-254083],[1192,-218,-547,78840],[1192,2166,0,-296288]],
}
def csc(p,enc,fmt):return tuple(max(0,min(255,(sum(a*b for a,b in zip(row,p))+row[3])//1024)) for row in mat[enc,fmt])
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
for enc in (1,2):
 for inf,outf,colors in [('RGB3','YU12',[(0,0,0),(255,255,255),(255,0,0),(0,255,0),(0,0,255),(31,93,177)]),('YU12','RGB3',[(16,128,128),(235,128,128),(100,90,200),(150,180,70)])]:
  for n,color in enumerate(colors):
   add(f'csc-{enc}-{inf}-{n}',inf,outf,w,h,w,h,lambda x,y,c=color:c,lambda x,y,c=csc(color,enc,inf):c,enc=enc,tolerance=1,note='Fixed-point programmed CSC matrix, clipped to byte; explicit 1 LSB rounding tolerance; solid chroma avoids phase ambiguity.')
for ow,oh in [(68,36),(16,8)]:
 add(f'scale-solid-{ow}x{oh}','RGB3','RGB3',w,h,ow,oh,lambda x,y:(41,137,219),lambda x,y:(41,137,219),tolerance=1,note='DC preservation only; 1 LSB rounding tolerance.')
# Large flat regions test spatial selection away from filter support and borders.
w,h=128,96
colors=[(30,60,90),(200,40,80),(80,190,50),(150,170,210)]
for ow,oh in [(256,192),(64,48)]:
 add(f'scale-quadrants-{ow}x{oh}','RGB3','RGB3',w,h,ow,oh,lambda x,y:colors[(y>=h//2)*2+(x>=w//2)],lambda x,y:colors[(y>=oh//2)*2+(x>=ow//2)],tolerance=1,masked=lambda x,y: min(x,ow-1-x,abs(x-ow//2),y,oh-1-y,abs(y-oh//2))>8,note='Only interiors compared; filter phase, transition ringing and exact impulse response remain unqualified.')
(P/'manifest.json').write_text(json.dumps(dict(cases=cases),indent=2)+'\n')
(P/'cases.tsv').write_text(''.join(
    c['name'] + '\t' + '\t'.join(map(str, c['args'])) + '\t' + str(c['tolerance']) + '\n'
    for c in cases))
print(len(cases),'cases generated')

// SPDX-License-Identifier: GPL-2.0-only
/* Bounded single-buffer V4L2 mem2mem qualification runner. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
struct queue { enum v4l2_buf_type type; struct v4l2_pix_format f; void *map; size_t len; int allocated, streaming; };
static int fd=-1;
static struct queue q[2]={{.type=V4L2_BUF_TYPE_VIDEO_OUTPUT},{.type=V4L2_BUF_TYPE_VIDEO_CAPTURE}};
static void cleanup(void) {
 for(int i=1;i>=0;i--) { if(q[i].streaming) ioctl(fd,VIDIOC_STREAMOFF,&q[i].type); if(q[i].map && q[i].map!=MAP_FAILED) munmap(q[i].map,q[i].len); if(q[i].allocated) { struct v4l2_requestbuffers r={.type=q[i].type,.memory=V4L2_MEMORY_MMAP}; ioctl(fd,VIDIOC_REQBUFS,&r); } } if(fd>=0) close(fd);
}
static void fail(const char *s) { perror(s); exit(1); }
static void check(int v,const char*s) { if(v<0) fail(s); }
static int xio(unsigned long op,void*p) { int r; do r=ioctl(fd,op,p); while(r<0 && errno==EINTR); return r; }
static unsigned num(const char*s) { char *e; unsigned long v=strtoul(s,&e,10); if(!*s||*e||v>8192) { errno=EINVAL; fail("number"); } return v; }
static void control(unsigned id,int value) { struct v4l2_control c={.id=id,.value=value}; check(xio(VIDIOC_S_CTRL,&c),"S_CTRL"); }
static void setup(struct queue *v,const char *fmt,unsigned w,unsigned h,unsigned enc) {
 if(strlen(fmt)!=4) { errno=EINVAL; fail("fourcc"); }
 struct v4l2_format f={.type=v->type};
 f.fmt.pix=(struct v4l2_pix_format){.width=w,.height=h,.pixelformat=v4l2_fourcc(fmt[0],fmt[1],fmt[2],fmt[3]),.field=V4L2_FIELD_NONE,.colorspace=enc==1?V4L2_COLORSPACE_SMPTE170M:V4L2_COLORSPACE_REC709,.ycbcr_enc=enc,.quantization=!strcmp(fmt,"RGB3")?V4L2_QUANTIZATION_FULL_RANGE:V4L2_QUANTIZATION_LIM_RANGE};
 check(xio(VIDIOC_S_FMT,&f),"S_FMT"); v->f=f.fmt.pix;
 if(v->f.width!=w||v->f.height!=h||v->f.pixelformat!=v4l2_fourcc(fmt[0],fmt[1],fmt[2],fmt[3])) { errno=EINVAL; fail("format changed"); }
 printf("format type=%u fourcc=%s width=%u height=%u stride=%u size=%u colorspace=%u enc=%u quant=%u\n",v->type,fmt,w,h,v->f.bytesperline,v->f.sizeimage,v->f.colorspace,v->f.ycbcr_enc,v->f.quantization);
}
static void allocate(struct queue*v) {
 struct v4l2_requestbuffers r={.count=1,.type=v->type,.memory=V4L2_MEMORY_MMAP}; check(xio(VIDIOC_REQBUFS,&r),"REQBUFS"); v->allocated=1;
 if(!r.count) { errno=ENOMEM; fail("zero buffers"); }
 struct v4l2_buffer b={.type=v->type,.memory=V4L2_MEMORY_MMAP}; check(xio(VIDIOC_QUERYBUF,&b),"QUERYBUF"); v->len=b.length;
 if(v->len<v->f.sizeimage) { errno=EINVAL; fail("short buffer"); }
 v->map=mmap(NULL,v->len,PROT_READ|PROT_WRITE,MAP_SHARED,fd,b.m.offset); if(v->map==MAP_FAILED) fail("mmap"); memset(v->map,0xa5,v->len);
}
static void read_exact(const char *p,void*buf,size_t len) { FILE*f=fopen(p,"rb"); if(!f)fail(p); if(fread(buf,1,len,f)!=len||fgetc(f)!=EOF) { errno=EINVAL; fail("input file length"); } if(fclose(f))fail("fclose"); }
static void write_new(const char*p,void*buf,size_t len) { int out=open(p,O_WRONLY|O_CREAT|O_EXCL,0600); check(out,p); size_t done=0; while(done<len) { ssize_t n=write(out,(char*)buf+done,len-done); if(n<0&&errno==EINTR)continue; if(n<=0)fail("write"); done+=n; } check(close(out),"close output"); }
static long long now(void) { struct timespec t; check(clock_gettime(CLOCK_MONOTONIC,&t),"clock"); return (long long)t.tv_sec*1000+t.tv_nsec/1000000; }
int main(int argc,char**argv) {
 if(argc!=15) { fprintf(stderr,"usage: %s /dev/videoN expected-bus RGB3|YU12 RGB3|YU12 iw ih ow oh rotate hflip vflip enc(1=601,2=709) input-file output-file\n",argv[0]); return 2; }
 unsigned w=num(argv[5]),h=num(argv[6]),ow=num(argv[7]),oh=num(argv[8]),rot=num(argv[9]),hf=num(argv[10]),vf=num(argv[11]),enc=num(argv[12]);
 if(w<8||h<8||ow<8||oh<8||rot>270||rot%90||hf>1||vf>1||(enc!=1&&enc!=2)) return 2;
 atexit(cleanup); fd=open(argv[1],O_RDWR|O_NONBLOCK|O_CLOEXEC); check(fd,"open device");
 struct stat st; check(fstat(fd,&st),"fstat"); if(!S_ISCHR(st.st_mode)) { errno=ENODEV; fail("not character device"); }
 struct v4l2_capability c={0}; check(xio(VIDIOC_QUERYCAP,&c),"QUERYCAP");
 printf("driver=%.*s card=%.*s bus=%.*s caps=%08x device_caps=%08x\n",16,c.driver,32,c.card,32,c.bus_info,c.capabilities,c.device_caps);
 unsigned caps=c.capabilities&V4L2_CAP_DEVICE_CAPS?c.device_caps:c.capabilities;
 if(strncmp((char*)c.driver,"sun8i-rotate",sizeof(c.driver))||strncmp((char*)c.bus_info,argv[2],sizeof(c.bus_info))||(caps&(V4L2_CAP_VIDEO_M2M|V4L2_CAP_STREAMING))!=(V4L2_CAP_VIDEO_M2M|V4L2_CAP_STREAMING)) { errno=ENODEV; fail("device identity/capability mismatch"); }
 for(int i=0;i<2;i++) for(unsigned j=0;;j++) { struct v4l2_fmtdesc f={.index=j,.type=q[i].type}; if(xio(VIDIOC_ENUM_FMT,&f)<0) { if(errno==EINVAL)break; fail("ENUM_FMT"); } printf("enum type=%u fourcc=%c%c%c%c\n",f.type,f.pixelformat&255,(f.pixelformat>>8)&255,(f.pixelformat>>16)&255,(f.pixelformat>>24)&255); }
 setup(&q[0],argv[3],w,h,enc); control(V4L2_CID_ROTATE,rot); control(V4L2_CID_HFLIP,hf); control(V4L2_CID_VFLIP,vf); setup(&q[1],argv[4],ow,oh,enc);
 for(int i=0;i<2;i++) allocate(&q[i]);
 read_exact(argv[13],q[0].map,q[0].f.sizeimage);
 void *original=malloc(q[0].len); if(!original)fail("malloc"); memcpy(original,q[0].map,q[0].len);
 for(int i=0;i<2;i++) { struct v4l2_buffer b={.type=q[i].type,.memory=V4L2_MEMORY_MMAP,.bytesused=i?0:q[i].f.sizeimage}; check(xio(VIDIOC_QBUF,&b),"QBUF"); }
 for(int i=0;i<2;i++) { check(xio(VIDIOC_STREAMON,&q[i].type),"STREAMON"); q[i].streaming=1; }
 int done[2]={0,0}; long long deadline=now()+5000;
 while(!done[0]||!done[1]) {
  for(int i=0;i<2;i++) if(!done[i]) { struct v4l2_buffer b={.type=q[i].type,.memory=V4L2_MEMORY_MMAP}; int r=xio(VIDIOC_DQBUF,&b); if(r==0) { if(b.index||b.flags&V4L2_BUF_FLAG_ERROR||b.bytesused!=q[i].f.sizeimage) { errno=EIO; fail("DQBUF status/index/payload"); } done[i]=1; printf("dequeue type=%u sequence=%u bytes=%u flags=%08x\n",b.type,b.sequence,b.bytesused,b.flags); } else if(errno!=EAGAIN)fail("DQBUF"); }
  if(done[0]&&done[1])break;
  int ms=(int)(deadline-now()); if(ms<=0) { errno=ETIMEDOUT; fail("completion deadline"); }
  struct pollfd p={.fd=fd,.events=(done[1]?0:POLLIN)|(done[0]?0:POLLOUT)}; int r=poll(&p,1,ms); if(r<0&&errno==EINTR)continue; check(r,"poll"); if(!r) { errno=ETIMEDOUT; fail("poll timeout"); } if(p.revents&(POLLERR|POLLHUP|POLLNVAL)) { errno=EIO; fail("poll status"); }
 }
 for(int i=1;i>=0;i--) { check(xio(VIDIOC_STREAMOFF,&q[i].type),"STREAMOFF"); q[i].streaming=0; }
 if(memcmp(original,q[0].map,q[0].len)) { errno=EIO; fail("input changed"); } free(original);
 for(size_t i=q[1].f.sizeimage;i<q[1].len;i++) if(((uint8_t*)q[1].map)[i]!=0xa5) { errno=EIO; fail("capture tail canary changed"); }
 printf("input unchanged; capture tail canary bytes=%zu unchanged; mapped tail is not an IOMMU guard\n",q[1].len-q[1].f.sizeimage);
 write_new(argv[14],q[1].map,q[1].f.sizeimage); return 0;
}

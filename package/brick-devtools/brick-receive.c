// SPDX-License-Identifier: GPL-2.0-only
/* Bounded serial receiver; writes only new files directly under /tmp. */
#define _GNU_SOURCE
#include <unistd.h>
#include <sys/syscall.h>
#include <errno.h>
static long call6(long n,long a,long b,long c,long d,long e,long f) {
 long r=syscall(n,a,b,c,d,e,f); return r<0?-errno:r;
}
#define NR_READ SYS_read
#define NR_WRITE SYS_write
#define NR_OPEN SYS_openat
#define NR_CLOSE SYS_close
#define NR_IOCTL SYS_ioctl
#define NR_POLL SYS_ppoll
#define NR_CLOCK SYS_clock_gettime
#define NR_UNLINK SYS_unlinkat
#define NR_SIGMASK SYS_rt_sigprocmask

struct tty_mode { unsigned in,out,control,local; unsigned char line,cc[19]; };
struct pollfd { int fd; short events,revents; };
static unsigned char buffer[4096];
static long now_ms(void) {
 long t[2]; if(call6(NR_CLOCK,1,(long)t,0,0,0,0)<0)return -1;
 return t[0]*1000+t[1]/1000000;
}
static int wait_fd(int fd,int writing,long end) {
 for(;;) {
  long now=now_ms(),left=end-now; if(now<0||left<=0)return -1;
  long ts[2]={left/1000,(left%1000)*1000000};
  struct pollfd p={fd,writing?4:1,0};
  long rc=call6(NR_POLL,(long)&p,1,(long)ts,0,8,0);
  if(rc==-4)continue;
  if(rc<=0||p.revents&(8|16|32))return -1;
  if(p.revents&p.events)return 0;
 }
}
static int send_marker(const char *name,unsigned count) {
 char b[48];unsigned n=0;
 while(*name)b[n++]=*name++;
 b[n++]=' ';
 for(int i=7;i>=0;i--){unsigned h=(count>>(i*4))&15;b[n++]=h<10?'0'+h:'a'+h-10;}
 b[n++]='\n';unsigned sent=0;long deadline=now_ms()+2000;
 while(sent<n){if(wait_fd(1,1,deadline))return -1;long r=call6(NR_WRITE,1,(long)(b+sent),n-sent,0,0,0);if(r==-4)continue;if(r<=0)return -1;sent+=r;}
 return 0;
}
static int receiver(int argc,char **argv) {
 if(argc!=4)return 2;
 char *path=argv[1];
 const char *prefix="/tmp/";for(unsigned i=0;i<5;i++)if(path[i]!=prefix[i])return 2;
 unsigned plen=5;for(;path[plen];plen++)if(path[plen]=='/'||plen>180)return 2;
 if(plen==5||(plen==6&&path[5]=='.')||(plen==7&&path[5]=='.'&&path[6]=='.'))return 2;
 unsigned size=0;char *p=argv[2];if(!*p)return 2;
 for(;*p;p++){if(*p<'0'||*p>'9'||size>6710886)return 2;size=size*10+*p-'0';}
 if(size>67108864)return 2;
 unsigned seconds=0;p=argv[3];if(!*p)return 2;
 for(;*p;p++){if(*p<'0'||*p>'9'||seconds>60)return 2;seconds=seconds*10+*p-'0';}
 if(seconds<1||seconds>600)return 2;
 struct tty_mode old,raw;
 if(call6(NR_IOCTL,0,0x5401,(long)&old,0,0,0)<0)return 3;
 unsigned long mask=~0UL,previous;
 if(call6(NR_SIGMASK,0,(long)&mask,(long)&previous,8,0,0)<0)return 3;
 /* O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW. No overwrite or symlink traversal. */
 long file=call6(NR_OPEN,-100,(long)path,1|64|128|131072,0600,0,0);
 int status=4,changed=0;unsigned received=0;
 if(file<0)goto restore_mask;
 raw=old;raw.in=0;raw.out=0;raw.local=0;raw.control=(old.control&~(0x30|0x100))|0x30|0x80;
 raw.cc[6]=1;raw.cc[5]=0; /* VMIN1, VTIME0 */
 if(call6(NR_IOCTL,0,0x5402,(long)&raw,0,0,0)<0)goto cleanup;
 changed=1;
 if(send_marker("BRX_READY",size))goto cleanup;
 long deadline=now_ms()+seconds*1000;
 while(received<size) {
  unsigned chunk=size-received;if(chunk>sizeof buffer)chunk=sizeof buffer;
  unsigned got=0;
  while(got<chunk){if(wait_fd(0,0,deadline))goto cleanup;long n=call6(NR_READ,0,(long)(buffer+got),chunk-got,0,0,0);if(n==-4)continue;if(n<=0)goto cleanup;got+=n;}
  unsigned wrote=0;
  while(wrote<got){long n=call6(NR_WRITE,file,(long)(buffer+wrote),got-wrote,0,0,0);if(n==-4)continue;if(n<=0)goto cleanup;wrote+=n;}
  received+=got;
  if(send_marker("BRX_ACK",received))goto cleanup;
 }
 status=0;
cleanup:
 if(call6(NR_CLOSE,file,0,0,0,0,0)<0)status=4;
 if(changed && call6(NR_IOCTL,0,0x5402,(long)&old,0,0,0)<0)status=5;
 if(status)call6(NR_UNLINK,-100,(long)path,0,0,0,0);
 /* Terminal has been restored before DONE, including on transfer failure. */
 send_marker(status?"BRX_FAIL":"BRX_DONE",received);
restore_mask:
 call6(NR_SIGMASK,2,(long)&previous,0,8,0,0);
 return status;
}
int main(int argc,char **argv){return receiver(argc,argv);}

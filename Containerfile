FROM registry.fedoraproject.org/fedora:44
RUN dnf -y install gcc gcc-c++ make binutils bash patch gzip bzip2 perl \
    perl-English perl-ExtUtils-MakeMaker perl-Thread-Queue perl-FindBin perl-IPC-Cmd \
    tar cpio unzip rsync file bc findutils which diffutils git wget curl \
    flex bison openssl-devel ncurses-devel python3 xz zlib-devel \
    && dnf clean all

RUN dnf -y install cmake && dnf clean all

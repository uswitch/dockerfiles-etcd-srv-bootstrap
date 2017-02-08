FROM python:3.6-alpine
MAINTAINER Tom Taylor <tom.taylor@uswitch.com>

EXPOSE 2379 2380
ENV ETCD_VER=v3.1.0

RUN apk --update --no-cache --virtual .builddeps add curl tar && \
    curl -Lso etcd-${ETCD_VER}-linux-amd64.tar.gz https://github.com/coreos/etcd/releases/download/${ETCD_VER}/etcd-${ETCD_VER}-linux-amd64.tar.gz && \
    tar zxf etcd-${ETCD_VER}-linux-amd64.tar.gz  etcd-${ETCD_VER}-linux-amd64/etcd && \
    mv etcd-${ETCD_VER}-linux-amd64/etcd / && \
    rm -rf etcd-${ETCD_VER}-linux-amd64.tar.gz etcd-${ETCD_VER}-linux-amd64/ && \
    pip install boto3 requests && \
    apk del .builddeps

COPY etcd-boot.py /

ENTRYPOINT ["/etcd-boot.py"]

FROM python:2.7
MAINTAINER houjie <deffyc@gmail.com>

ARG LISTEN_USERNAME="admin"
ARG LISTEN_PASSWORD="admin"
ARG LISTEN_PORT=8087
ENV LISTEN_USERNAME $LISTEN_USERNAME
ENV LISTEN_PASSWORD $LISTEN_PASSWORD
ENV LISTEN_PORT $LISTEN_PORT

RUN mkdir /xx && \
   wget -O /xx/ip_checked.txt https://raw.githubusercontent.com/XX-net/XX-Net/master/code/default/gae_proxy/local/ip_checked.txt & \
   cd /xx
# add all repo
ADD ./ /xx

# run test
WORKDIR /xx
RUN pip install pyOpenSSL

VOLUME ["/xx"]

EXPOSE 8086 8087


CMD  python proxy.py

FROM python:2.7
MAINTAINER houjie <deffyc@gmail.com>

ARG LISTEN_USERNAME=
ARG LISTEN_PASSWORD=
ENV LISTEN_USERNAME $LISTEN_USERNAME
ENV LISTEN_PORT $LISTEN_PASSWORD

RUN mkdir /xx && \
   cd /xx
# add all repo
ADD ./ /xx

# run test
WORKDIR /xx
RUN pip install pyOpenSSL

VOLUME ["/xx"]

EXPOSE 8086 8087


CMD  python proxy.py

FROM ubuntu:16.04

MAINTAINER Bitworks Software info@bitworks.software

ENV KVM_HOST qemu+tcp://root@10.252.1.35:16509/system
ENV KAFKA_BOOTSTRAP localhost:9092
ENV KAFKA_TOPIC kvm-metrics

ENV PAUSE 20
ENV GATHER_HOST_STATS true
ENV LOGLEVEL DEBUG

ENV CS_ENDPOINT http://localhost/client/api
ENV CS_API_KEY key
ENV CS_SECRET_KEY secret
ENV VOLUMES_UPDATE_INTERVAL 600

RUN echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections
RUN DEBIAN_FRONTEND=noninteractive apt-get update
RUN DEBIAN_FRONTEND=noninteractive apt-get install -y -q python-libvirt python-pip openssh-client

COPY ./src /opt
COPY requirements.txt /opt

RUN pip install --upgrade pip
RUN pip install -r /opt/requirements.txt



WORKDIR /opt

CMD ["/bin/bash", "/opt/virt-hostinfo.py"]



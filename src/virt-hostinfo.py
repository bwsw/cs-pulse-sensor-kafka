#!/usr/bin/python
# -*- coding: UTF-8 -*-


import json
import libvirt
import os
import sys
import traceback
import logging
from datetime import datetime
from xml.etree import ElementTree
from cs import CloudStack
from repoze.lru import lru_cache

from kafka import KafkaProducer

MB = 1024 * 1024
GB = 1024 * MB
SEC = 1000000000.

kvm_host = os.environ["KVM_HOST"]
kafka_bootstrap_servers = os.environ["KAFKA_BOOTSTRAP"]
kafka_topic = os.environ["KAFKA_TOPIC"]
gather_host_stats = os.environ["GATHER_HOST_STATS"]

cs_endpoint = os.environ['CS_ENDPOINT']
cs_api_key = os.environ['CS_API_KEY']
cs_secret_key = os.environ['CS_SECRET_KEY']

loglevel = os.environ["LOGLEVEL"]

FORMAT = '%(asctime)-15s %(message)s'
logging.basicConfig(format=FORMAT, stream=sys.stderr, level=getattr(logging, loglevel))

producer = KafkaProducer(bootstrap_servers=kafka_bootstrap_servers.split(","),
                         value_serializer=lambda m: json.dumps(m).encode('ascii'),
                         retries=5)

cs = CloudStack(endpoint=cs_endpoint,
                key=cs_api_key,
                secret=cs_secret_key)


@lru_cache(timeout=60, maxsize=100)
def get_projects():
    projects = cs.listProjects(listall=True, filter='id')
    if len(projects) > 0:
        return [rec['id'] for rec in projects['project']]
    else:
        return []

@lru_cache(timeout=3600, maxsize=100000)
def get_volume_uuid(vmname, vmuuid, path):
    def _iterate_over_projects():
        logging.debug("Volumes listing for VM %s in main scope" % vmuuid)
        vols = cs.listVolumes(listall=True, filter='path,id', virtualmachineid=vmuuid)
        logging.debug(json.dumps(vols))
        yield vols
        for p in get_projects():
            logging.debug("Volumes listing for VM %s for project = %s" % (vmuuid, p))
            vols = cs.listVolumes(listall=True, filter='path,id', virtualmachineid=vmuuid, projectid=p)
            logging.debug(json.dumps(vols))
            yield vols

    for vols in _iterate_over_projects():
        if len(vols) > 0:
            for v in vols['volume']:
                if v['path'] == path:
                    logging.info("Volume path for VM %s mapping found (id: path) = (%s, %s, %s)" % (vmname, v['id'], path, 'X' if v['id'] != path else 'O'))
                    return v['id']
            logging.info("Volume path for VM %s mapping not found for path = %s" % (vmname, path))
            return path
    logging.info("Volume path for VM %s mapping not found for path = %s" % (vmname, path))
    return path


conn = libvirt.open(kvm_host)

if conn == None:
    raise Exception("Failed to open connection to %s" % (kvm_host,))
    exit(1)

while True:
    try:
        if gather_host_stats == 'true':
            current_time = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            query = []
            stats = conn.getCPUStats(libvirt.VIR_NODE_CPU_STATS_ALL_CPUS)
            nodeinfo = conn.getInfo()
            mem = conn.getFreeMemory()
            query.append({
                "measurement": "nodeInfo",
                "time": current_time,
                "tags": {'vmHost': kvm_host},
                "fields": {
                    'kernel': long(stats['kernel'] / SEC),
                    'idle': long(stats['idle'] / SEC),
                    'user': long(stats['user'] / SEC),
                    'iowait': long(stats['iowait'] / SEC),
                    'totalMem': nodeinfo[1],
                    'freeMem': long(mem / MB)
                }
            })

        domainIDs = conn.listDomainsID()
        if domainIDs == None:
            raise Exception('Failed to get a list of domain IDs')

        for domainID in domainIDs:
            current_time = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

            domain = conn.lookupByID(domainID)
            # print [method for method in dir(domain) if callable(getattr(domain, method))]
            # continue

            tree = ElementTree.fromstring(domain.XMLDesc())

            vm_host = {}
            vm_host["host"] = kvm_host
            vm_host["name"] = domain.name()
            vm_host["uuid"] = domain.UUIDString()
            state, maxmem, mem, cpus, cput = domain.info()
            vm_host["maxmem"] = maxmem
            vm_host["mem"] = mem
            vm_host["cpus"] = cpus
            vm_host["cputime"] = str(cput / SEC)
            vm_host["isrunning"] = domain.isActive()
            vm_host["rss"] = long(domain.memoryStats()["rss"])

            vm_host["networkStatistics"] = {}
            net_devs = tree.findall('devices/interface')
            for intf in net_devs:
                dev = intf.find('target').get('dev')
                addr = intf.find('mac').get('address')
                stats = domain.interfaceStats(dev)
                vm_host["networkStatistics"][addr] = {
                    "readBytes": long(stats[0]),
                    "readPackets": long(stats[1]),
                    "readErrors": long(stats[2]),
                    "readDrops": long(stats[3]),
                    "writeBytes": long(stats[4]),
                    "writePackets": long(stats[5]),
                    "writeErrors": long(stats[6]),
                    "writeDrops": long(stats[7])}

            vm_host["diskStatistics"] = {}
            block_devs = tree.findall('devices/disk')
            for disk in block_devs:
                if disk.get('device') <> 'disk':
                    continue

                dev = disk.find('target').get('dev')
                stats = domain.blockStats(dev)

                total_space, allocated_space, disk_space = domain.blockInfo(dev)
                # print info[0] / 1024 / 1024 / 1024, info[1] / 1024 / 1024 / 1024, info[2] / 1024 / 1024 / 1024

                source = None
                if disk.get('type') == 'file':
                    source = disk.find('source').get('file')
                    source = "file://%s" % (source,)
                elif disk.get('type') == 'block':
                    source = disk.find('source').get('dev')
                    source = "block://%s" % (source,)
                elif disk.get('type') == 'network' and disk.find('source').get('protocol') == 'rbd':
                    source = disk.find('source').get('name')
                    source = "rbd://%s" % (source,)
                else:
                    raise Exception("Undefined or Unknown device type for VM: %s." % (vm_host["name"],))

                if source is not None:
                    vm_host["diskStatistics"][source] = {
                        "readIOPS": long(stats[0]),
                        "readBytes": long(stats[1]),
                        "writeIOPS": long(stats[2]),
                        "writeBytes": long(stats[3]),
                        "ioErrors": long(stats[4]),
                        "totalSpace": int(total_space / GB),
                        "allocatedSpace": int(allocated_space / GB),
                        "onDiskSpace": int(disk_space / GB)}

            tags = {
                "vmId": vm_host["name"],
                "vmUuid": vm_host["uuid"],
                "vmHost": vm_host["host"],
            }
            query = [
                {
                    "measurement": "cpuTime",
                    "time": current_time,
                    "tags": tags,
                    "fields": {
                        "cpuTime": float(vm_host["cputime"]),
                        "cpus": int(vm_host["cpus"])
                    }
                },
                {
                    "measurement": "rss",
                    "time": current_time,
                    "tags": tags,
                    "fields": {
                        "rss": long(vm_host["rss"]),
                        "mem": long(vm_host["mem"]),
                        "maxmem": long(vm_host["maxmem"])
                    }
                },
            ]

            for key in vm_host["networkStatistics"].keys():
                net_tags = tags.copy()
                net_tags["mac"] = key

                query.append({
                    "measurement": "networkInterface",
                    "time": current_time,
                    "tags": net_tags,
                    "fields": vm_host["networkStatistics"][key]
                })

            for path in vm_host["diskStatistics"].keys():
                parts = path.split('/')
                disk_tags = tags.copy()
                disk_tags['image-path'] = parts[-1]
                disk_tags['image'] = get_volume_uuid(vm_host["name"], vm_host['uuid'], parts[-1])
                disk_tags['pool'] = parts[-2]
                query.append({
                    "measurement": "disk",
                    "time": current_time,
                    "tags": disk_tags,
                    "fields": vm_host["diskStatistics"][path]
                })

            producer.send(kafka_topic, query)

            if loglevel == 'true':
                print json.dumps(query, sort_keys=True, indent=4, separators=(',', ': '))
                print "\n"

            producer.send(kafka_topic, query)

            # logging.debug(json.dumps(query, sort_keys=True, indent=4, separators=(',', ': ')))

        producer.flush()

    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        exit(1)

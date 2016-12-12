#!/usr/bin/env python
import requests
import boto3
import json
from sys import argv, exit
from os import execve
from time import sleep


def hexify(ipv4):
  return ''.join(["{:02x}".format(int(q)) for q in ipv4.split('.')])


class MetaData(object):
  url = 'http://169.254.169.254/latest/meta-data/'

  @staticmethod
  def _get_text(path):
    url = MetaData.url + path
    r = requests.get(MetaData.url + path)
    if r.status_code != 200:
      return None
    return r.text

  @property
  def instance_id(self):
    return MetaData._get_text('instance-id')

  @property
  def region(self):
    az = MetaData._get_text('placement/availability-zone')
    return az[:len(az) - 1]

  @property
  def private_ipv4(self):
    return MetaData._get_text('local-ipv4')


class Instance(object):
  def __init__(self, id, region):
    self.ec2 = boto3.client('ec2', region_name=region)
    self.instance = self.ec2.describe_instances(InstanceIds=[id])

  @property
  def tags(self):
    return self.instance['Reservations'][0]['Instances'][0]['Tags']

  @property
  def asg(self):
    return filter(lambda x: x['Key'] == 'aws:autoscaling:groupName', self.tags)[0]['Value']


class Asg(object):
  def __init__(self, name, region):
    self.ec2 = boto3.client('ec2', region_name=region)
    self.filters = [
      {'Name': 'tag:aws:autoscaling:groupName', 'Values': [name]}
    ]
    self.name = name
    self.region = region

  @property
  def members(self):
    return self.ec2.describe_instances(Filters=self.filters)

  @property
  def ipv4s(self):
    instances = [i for i in [r['Instances'] for r in self.members['Reservations']]]
    running = filter(lambda x: x['State']['Name'] in ('running', 'pending'), instances)
    return [i['PrivateIpAddress'] for i in running]


class Zone(object):
  def __init__(self, name):
    self.name = name
    self.client = boto3.client('route53')
    all_zones = self.client.list_hosted_zones_by_name()['HostedZones']

    labels = []
    self.zone = None
    zone_name = name.split('.')
    for i in range(len(zone_name)):
      zone = filter(lambda x: x['Name'].rstrip('.') == '.'.join(zone_name[i:]), all_zones)
      if len(zone) == 1:
        self.zone = zone[0]
        self.zone_name = '.'.join(zone_name[i:])
        break
      labels.append(zone_name[i])
    self.labels = '.'.join(labels)

  @staticmethod
  def reverse(domain):
    return '.'.join(reversed(domain.split()))

  @property
  def id(self):
    return self.zone['Id'][12:]

  @staticmethod
  def change_batch(action, name, rrtype, rr, ttl=60):
    return {
      'Changes': [
        {
          'Action': action,
          'ResourceRecordSet': {
            'Name': name,
            'Type': rrtype,
            'TTL': ttl,
            'ResourceRecords': rr
          }
        }
      ]
    }

  def updateA(self, name, *hosts):
    batch = Zone.change_batch(
      action='UPSERT',
      name='.'.join((name, self.labels, self.zone_name)),
      rrtype='A',
      ttl=60,
      rr=[{'Value': host} for host in hosts]
    )
    print(json.dumps(batch))
    print(self.client.change_resource_record_sets(
      HostedZoneId = self.id,
      ChangeBatch = batch
    ))

  def deleteA(self, name, *hosts):
    batch = Zone.change_batch(
      action='DELETE',
      name='.'.join((name, self.labels, self.zone_name)),
      rrtype='A',
      rr=[{'Value': host} for host in hosts]
    )
    print(json.dumps(batch))
    print(self.client.change_resource_record_sets(
      HostedZoneId = self.id,
      ChangeBatch = batch
    ))


  def updateSRV(self, name, *hosts):
    batch = Zone.change_batch(
      action='UPSERT',
      name='.'.join((name, self.labels, self.zone_name)),
      rrtype='SRV',
      ttl=60,
      rr=[{'Value': host} for host in hosts]
    )
    print(json.dumps(batch))
    print(self.client.change_resource_record_sets(
      HostedZoneId = self.id,
      ChangeBatch = batch
    ))


if __name__ == '__main__':
  if len(argv) != 4 or argv[1] not in ('up', 'down'):
    print("Usage: <up|down> <prefix> <domain>\ne.g up etcd example.com")
    exit(101)
  prefix = argv[2]
  domain = argv[3]

  m = MetaData()
  i = Instance(m.instance_id, m.region)
  asg = Asg(i.asg, m.region)
  z = Zone(domain)

  if argv[1] == 'up':
    for ip in asg.ipv4s:
      my_name = "{}-{}".format(prefix, hexify(ip))
      z.updateA(my_name, ip)

    z.updateSRV('_etcd-server._tcp', *["0 0 2380 {}-{}.{}".format(prefix, hexify(ip), z.name) for ip in asg.ipv4s])
    z.updateSRV('_etcd-client._tcp', *["0 0 2379 {}-{}.{}".format(prefix, hexify(ip), z.name) for ip in asg.ipv4s])

    sleep(60) # Artificial delay for Amazons eventually consistent DNS

    new_env = {
            'ETCD_ADVERTISE_CLIENT_URLS': 'http://{}:2379'.format(m.private_ipv4),
            'ETCD_INITIAL_ADVERTISE_PEER_URLS': 'http://{}.{}:2380'.format(my_name,domain),
            'ETCD_LISTEN_PEER_URLS': "http://0.0.0.0:2380",
            'ETCD_LISTEN_CLIENT_URLS': "http://0.0.0.0:2379",
            'ETCD_DISCOVERY_SRV': domain
    }

    execve('/etcd', ('etcd',), new_env)

  elif argv[1] == 'down':
    z.deleteA("{}-{}".format(prefix, hexify(m.private_ipv4)), m.private_ipv4)


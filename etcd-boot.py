#!/usr/bin/env python
import argparse
from sys import argv, exit
from os import execve, getenv
from time import sleep
import requests
import boto3
import json


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
        return [x['Value'] for x in self.tags if x['Key'] == 'aws:autoscaling:groupName'][0]


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
        instances = sum([i for i in (r['Instances'] for r in self.members['Reservations'])], [])
        running = [x for x in instances if x['State']['Name'] in ('running', 'pending')]
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
            zone = [x for x in all_zones if x['Name'].rstrip('.') == '.'.join(zone_name[i:])]
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


class Etcd(object):
    def __init__(self, host, port=2379, scheme='https', ca=False, cert=None, key=None):
        self.ssl_params = {'verify': ca}
        if cert and key:
            self.ssl_params['cert'] = (cert, key)
        self.base_url = "{}://{}:{}".format(scheme, host, port)

    @staticmethod
    def membername(prefix, ip):
        return "{}-{}".format(prefix, hexify(ip))

    @staticmethod
    def peerurl(prefix, ip, domain):
        return "https://{}.{}:2380".format(Etcd.membername(prefix, ip) ,domain)

    def _url(self, path):
        return "{}/{}".format(self.base_url, path)

    def members(self):
        try:
            r = requests.get(self._url("v2/members"), **self.ssl_params)
            print("Got members from {} with {}".format(self.base_url, self.ssl_params))
            return r.json()['members']
        except (requests.ConnectionError, ValueError, TypeError) as e:
            print("Failed to get members from {} with {}.\n{}".format(self.base_url, self.ssl_params, e))
            return False

    def member_names(self):
        try:
            return [m['name'] for m in self.members()]
        except TypeError:
            return False

    def add(self, *peerURLs):
        url = self._url("v2/members")
        try:
            r = requests.post(url, json={'PeerURLs': peerURLs}, **self.ssl_params)
            print("Adding {} via {}, got {}".format(peerURLs, url, r.status_code))
            return r.status_code == 201
        except requests.ConnectionError:
            return False

    def remove(self, id):
        url = self._url("v2/members/{}".format(id))
        try:
            r = requests.delete(url, **self.ssl_params)
            print("Removing {} via {}, got {}".format(id, url, r.status_code))
            return r.status_code == 204
        except requests.ConnectionError:
            return False


if __name__ == '__main__':
    if len(argv) < 4 or argv[1] not in ('up', 'down'):
        print("Usage: <up|down> <prefix> <domain> [optional args to etcd]\ne.g up etcd example.com")
        exit(101)
    prefix = argv[2]
    domain = argv[3]
    etcd_args = argv[4:] if len(argv) > 4 else []

    etcd_ssl = dict(
        ca=getenv('CA'),
        cert=getenv('CERT'),
        key=getenv('KEY')
    )
    print("Got TLS settings from env ... \n{}".format(etcd_ssl))

    m = MetaData()
    i = Instance(m.instance_id, m.region)
    asg = Asg(i.asg, m.region)
    z = Zone(domain)
    my_name = "{}-{}".format(prefix, hexify(m.private_ipv4))
    my_peerurl = Etcd.peerurl(prefix, m.private_ipv4, domain)

    if argv[1] == 'up':
        # Individual A records
        for ip in sorted(asg.ipv4s):
            z.updateA("{}-{}".format(prefix, hexify(ip)), ip)
        # Shared A record
        z.updateA("{}".format(prefix), *asg.ipv4s)
        # SRV records
        z.updateSRV('_etcd-server-ssl._tcp', *["0 0 2380 {}-{}.{}".format(prefix, hexify(ip), z.name) for ip in sorted(asg.ipv4s)])
        z.updateSRV('_etcd-client-ssl._tcp', *["0 0 2379 {}-{}.{}".format(prefix, hexify(ip), z.name) for ip in sorted(asg.ipv4s)])

        # Try and get cluster status from an existing member and see if we are a member
        for ip in asg.ipv4s:
            e = Etcd(ip, **etcd_ssl)
            members = e.member_names()
            if members:
                print("Member up at {}".format(ip))
                if my_name in members:
                    print("I am a member, assuming new cluster")
                    cluster_state = "new"
                else:
                    print("I am not a member, assuming existing cluster")
                    cluster_state = "existing"
                break
        else:
            print("No peers were up, assuming new cluster")
            cluster_state = "new"

        # Clean up any nodes that shouldn't be here
        if cluster_state == "existing":
            asg_ips = asg.ipv4s
            # Are there any nodes that shouldn't be there?
            names_from_asg = [Etcd.membername(prefix, ip) for ip in asg_ips]
            for member in e.members():
                if member['name'] not in names_from_asg:
                    # Bad member found, removing
                    e.remove(member['id'])
            # Are there any nodes missing?
            names_from_etcd = [member['name'] for members in e.members()]
            for ip in asg_ips:
                if Etcd.membername(prefix, ip) not in names_from_etcd:
                    e.add(Etcd.peerurl(prefix, ip, domain))

        # if cluster_state == "existing":
        #     names = ["{}-{}".format(prefix, hexify(ip)) for ip in asg.ipv4s]
        #     for member in e.members():
        #         if member['name'] not in names:
        #             # Bad member found, removing
        #             e.remove(member['id'])
        #     # Add myself as a member
        #     e.add(my_peerurl)

        # Artificial Delay for slow Route53 updates :-(
        sleep(30)

        # Start etcd
        new_env = {
            'ETCD_NAME': "{}".format(my_name),
            'ETCD_DATA_DIR': "/etcd-data",
            'ETCD_INITIAL_CLUSTER_TOKEN': "{}.{}".format(prefix, domain),
            'ETCD_ADVERTISE_CLIENT_URLS': 'https://{}:2379'.format(m.private_ipv4),
            'ETCD_INITIAL_ADVERTISE_PEER_URLS': my_peerurl,
            'ETCD_LISTEN_PEER_URLS': "https://0.0.0.0:2380",
            'ETCD_LISTEN_CLIENT_URLS': "https://0.0.0.0:2379",
            'ETCD_DISCOVERY_SRV': domain,
            'ETCD_INITIAL_CLUSTER_STATE': cluster_state,
        }
        print("ETCD Environment:\n\n{}".format(json.dumps(new_env, indent=2)))

        execve('/etcd', ['etcd'] + etcd_args, new_env)

    elif argv[1] == 'down':
        z.deleteA("{}-{}".format(prefix, hexify(m.private_ipv4)), m.private_ipv4)

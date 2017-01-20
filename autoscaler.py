#!/usr/bin/env python3

import argparse
import collections
import logging
import os
import re
import time

import boto3
import pykube

FACTORS = {
    'm': 1 / 1000,
    'K': 1000,
    'M': 1000**2,
    'G': 1000**3,
    'T': 1000**4,
    'P': 1000**5,
    'E': 1000**6,
    'Ki': 1024,
    'Mi': 1024**2,
    'Gi': 1024**3,
    'Ti': 1024**4,
    'Pi': 1024**5,
    'Ei': 1024**6
}

RESOURCE_PATTERN = re.compile('^(\d*)(\D*)$')

RESOURCES = ['cpu', 'memory', 'pods']
DEFAULT_CONTAINER_REQUESTS = {'cpu': '10m', 'memory': '50Mi'}
DEFAULT_BUFFER_PERCENTAGE = {'cpu': 10, 'memory': 10, 'pods': 10}
DEFAULT_BUFFER_FIXED = {'cpu': '1', 'memory': '200Mi', 'pods': '10'}

logger = logging.getLogger('autoscaler')


def parse_resource(v: str):
    match = RESOURCE_PATTERN.match(v)
    factor = FACTORS.get(match.group(2), 1)
    return int(match.group(1)) * factor


def get_node_capacity_tuple(node: dict):
    capacity = node['capacity']
    return tuple(capacity[resource] for resource in RESOURCES)


def apply_buffer(requested: dict, buffer_percentage: dict, buffer_fixed: dict):
    requested_with_buffer = {}
    for resource, val in requested.items():
        requested_with_buffer[resource] = val * (1. + buffer_percentage[resource]/100) + buffer_fixed[resource]
    return requested_with_buffer


def find_weakest_node(nodes):
    return sorted(nodes, key=get_node_capacity_tuple)[0]


def is_sufficient(requested: dict, capacity: dict):
    for resource, cap in capacity.items():
        if requested.get(resource, 0) > cap:
            return False
    return True


def autoscale(buffer_percentage: dict, buffer_fixed: dict, dry_run: bool):
    try:
        config = pykube.KubeConfig.from_service_account()
    except FileNotFoundError:
        # local testing
        config = pykube.KubeConfig.from_file(os.path.expanduser('~/.kube/config'))
    api = pykube.HTTPClient(config)

    nodes = {}
    instances = {}
    region = None
    for node in pykube.Node.objects(api):
        region = node.labels['failure-domain.beta.kubernetes.io/region']
        zone = node.labels['failure-domain.beta.kubernetes.io/zone']
        instance_type = node.labels['beta.kubernetes.io/instance-type']
        capacity = {}
        for key, val in node.obj['status']['capacity'].items():
            capacity[key] = parse_resource(val)
        instance_id = node.obj['spec']['externalID']
        obj = {'zone': zone, 'instance_id': instance_id, 'instance_type': instance_type, 'capacity': capacity}
        nodes[node.name] = obj
        instances[instance_id] = obj

    nodes_by_asg_zone = collections.defaultdict(list)

    autoscaling = boto3.client('autoscaling', region)
    response = autoscaling.describe_auto_scaling_instances(InstanceIds=list(instances.keys()))
    for instance in response['AutoScalingInstances']:
        instances[instance['InstanceId']]['asg_name'] = instance['AutoScalingGroupName']
        key = instance['AutoScalingGroupName'], instance['AvailabilityZone']
        nodes_by_asg_zone[key].append(instances[instance['InstanceId']])

    pods = pykube.Pod.objects(api, namespace=pykube.all)

    usage_by_asg_zone = {}

    for pod in pods:
        node = nodes.get(pod.obj['spec'].get('nodeName'))
        if node:
            asg_name = node['asg_name']
            zone = node['zone']
        else:
            # pod is unassigned/pending
            asg_name = 'unknown'
            # TODO: we actually know the AZ by looking at volumes..
            zone = 'unknown'
        requests = collections.defaultdict(int)
        requests['pods'] = 1
        for container in pod.obj['spec']['containers']:
            container_requests = container['resources'].get('requests', {})
            for resource in RESOURCES:
                if resource != 'pods':
                    value = container_requests.get(resource)
                    if not value:
                        logger.warn('Container {}/{} has no resource request for {}'.format(
                                    pod.name, container['name'], resource))
                        value = DEFAULT_CONTAINER_REQUESTS[resource]
                    requests[resource] += parse_resource(value)
        key = asg_name, zone
        if key not in usage_by_asg_zone:
            usage_by_asg_zone[key] = {resource: 0 for resource in RESOURCES}
        for resource in usage_by_asg_zone[key]:
            usage_by_asg_zone[key][resource] += requests.get(resource, 0)

    asg_size = collections.defaultdict(int)

    for key, nodes in sorted(nodes_by_asg_zone.items()):
        asg_name, zone = key
        logger.info('{}/{}: current nodes: {}'.format(asg_name, zone, len(nodes)))
        requested = usage_by_asg_zone.get(key)
        pending = usage_by_asg_zone.get(('unknown', 'unknown'))
        if pending:
            for resource, val in pending.items():
                requested[resource] += val
        logger.info('{}/{}: requested resources: {}'.format(asg_name, zone, requested))
        requested_with_buffer = apply_buffer(requested, buffer_percentage, buffer_fixed)
        logger.info('{}/{}: requested with buffer: {}'.format(asg_name, zone, requested_with_buffer))
        # TODO: add requested resources from unassigned/pending pods
        weakest_node = find_weakest_node(nodes)
        required_nodes = 0
        capacity = {resource: 0 for resource in RESOURCES}
        while not is_sufficient(requested_with_buffer, capacity):
            for resource in capacity:
                capacity[resource] += weakest_node['capacity'][resource]
            required_nodes += 1

        logger.info('{}/{}: required nodes: {}'.format(asg_name, zone, required_nodes))
        asg_size[asg_name] += required_nodes

    asgs = {}
    response = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=list(asg_size.keys()))
    for asg in response['AutoScalingGroups']:
        asgs[asg['AutoScalingGroupName']] = asg

    for asg_name, desired_capacity in sorted(asg_size.items()):
        asg = asgs[asg_name]
        if desired_capacity != asg['DesiredCapacity']:
            if desired_capacity > asg['MaxSize']:
                logger.warn('Desired capacity for ASG {} is {}, but exceeds max {}'.format(
                            asg_name, desired_capacity, asg['MaxSize']))
            elif desired_capacity < asg['MinSize']:
                logger.warn('Desired capacity for ASG {} is {}, but is lower than min {}'.format(
                            asg_name, desired_capacity, asg['MinSize']))
            else:
                logger.info('Changing desired capacity for ASG {} from {} to {}..'.format(
                            asg_name, asg['DesiredCapacity'], desired_capacity))
                if dry_run:
                    logger.info('**DRY-RUN**: not performing any change')
                else:
                    autoscaling.set_desired_capacity(AutoScalingGroupName=asg_name,
                                                     DesiredCapacity=desired_capacity)


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', help='Dry run mode: do not change anything, just print what would be done',
                        action='store_true')
    parser.add_argument('--once', help='Run loop only once and exit', action='store_true')
    parser.add_argument('--interval', type=int, help='Loop interval', default=60)
    for resource in RESOURCES:
        parser.add_argument('--buffer-{}-percentage'.format(resource), type=int, help='{} buffer %%'.format(resource.capitalize()), default=DEFAULT_BUFFER_PERCENTAGE[resource])
        parser.add_argument('--buffer-{}-fixed'.format(resource), type=str, help='{} buffer (fixed amount)'.format(resource.capitalize()), default=DEFAULT_BUFFER_FIXED[resource])
    args = parser.parse_args()
    buffer_percentage = {}
    buffer_fixed = {}
    for resource in RESOURCES:
        buffer_percentage[resource] = getattr(args, 'buffer_{}_percentage'.format(resource))
        buffer_fixed[resource] = parse_resource(getattr(args, 'buffer_{}_fixed'.format(resource)))

    if args.dry_run:
        logger.info('**DRY-RUN**: no autoscaling will be performed!')

    while True:
        try:
            autoscale(buffer_percentage, buffer_fixed, dry_run=args.dry_run)
        except:
            logger.exception('Failed to autoscale')
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    main()

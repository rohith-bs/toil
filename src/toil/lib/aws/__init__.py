# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import collections
import inspect
import logging
import os
import re
import socket
import threading
from functools import lru_cache
from urllib.request import urlopen
from urllib.error import URLError

from typing import Any, Callable, Dict, Iterable, List, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

# This file isn't allowed to import anything that depends on Boto or Boto3,
# which may not be installed, because it has to be importable everywhere.

def get_current_aws_region() -> Optional[str]:
    """
    Return the AWS region that the currently configured AWS zone (see
    get_current_aws_zone()) is in.
    """
    aws_zone = get_current_aws_zone()
    return zone_to_region(aws_zone) if aws_zone else None

def get_aws_zone_from_environment() -> Optional[str]:
    """
    Get the AWS zone from TOIL_AWS_ZONE if set.
    """
    return os.environ.get('TOIL_AWS_ZONE', None)

def get_aws_zone_from_metadata() -> Optional[str]:
    """
    Get the AWS zone from instance metadata, if on EC2 and the boto module is
    available.
    """
    if running_on_ec2():
        try:
            import boto
            from boto.utils import get_instance_metadata
            return get_instance_metadata()['placement']['availability-zone']
        except (KeyError, ImportError):
            pass
    return None

def get_aws_zone_from_boto() -> Optional[str]:
    """
    Get the AWS zone from the Boto config file, if it is configured and the
    boto module is avbailable.
    """
    try:
        import boto
        zone = boto.config.get('Boto', 'ec2_region_name')
        if zone is not None:
            zone += 'a'  # derive an availability zone in the region
        return zone
    except ImportError:
        pass
    return None


def get_current_aws_zone() -> Optional[str]:
    """
    Get the currently configured or occupied AWS zone to use.

    Reports the TOIL_AWS_ZONE environment variable if set.

    Otherwise, if we have boto and are running on EC2, reports the zone we are
    running in.

    Finally, if we have boto2, and a default region is configured in Boto 2,
    chooses a zone in that region.

    Returns None if no method can produce a zone to use.
    """
    return get_aws_zone_from_environment() or \
        get_aws_zone_from_metadata() or \
        get_aws_zone_from_boto()

def zone_to_region(zone: str) -> str:
    """Get a region (e.g. us-west-2) from a zone (e.g. us-west-1c)."""
    # re.compile() caches the regex internally so we don't have to
    availability_zone = re.compile(r'^([a-z]{2}-[a-z]+-[1-9][0-9]*)([a-z])$')
    m = availability_zone.match(zone)
    if not m:
        raise ValueError(f"Can't extract region from availability zone '{zone}'")
    return m.group(1)

def running_on_ec2() -> bool:
    """
    Return True if we are currently running on EC2, and false otherwise.
    """
    # TODO: Move this to toil.lib.ec2 and make toil.lib.ec2 importable without boto?
    def file_begins_with(path, prefix):
        with open(path) as f:
            return f.read(len(prefix)) == prefix

    hv_uuid_path = '/sys/hypervisor/uuid'
    if os.path.exists(hv_uuid_path) and file_begins_with(hv_uuid_path, 'ec2'):
        return True
    # Some instances do not have the /sys/hypervisor/uuid file, so check the identity document instead.
    # See https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-identity-documents.html
    try:
        urlopen('http://169.254.169.254/latest/dynamic/instance-identity/document', timeout=1)
        return True
    except (URLError, socket.timeout):
        return False

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
import errno
import logging
import os
import socket
import sys
from typing import Any, Callable, ContextManager, Dict, Hashable, Iterable, Iterator, List, Optional, Union, cast
from urllib.parse import ParseResult

from toil.lib.aws import session
from toil.lib.misc import printq
from toil.lib.retry import (
    retry,
    old_retry,
    get_error_status,
    get_error_code,
    DEFAULT_DELAYS,
    DEFAULT_TIMEOUT
)

if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

try:
    from boto.exception import BotoServerError, S3ResponseError
    from botocore.exceptions import ClientError
    from mypy_boto3_s3 import S3Client, S3ServiceResource
    from mypy_boto3_s3.literals import BucketLocationConstraintType
    from mypy_boto3_s3.service_resource import Bucket, Object
    from mypy_boto3_sdb import SimpleDBClient
    from mypy_boto3_iam import IAMClient, IAMServiceResource
except ImportError:
    BotoServerError = None  # type: ignore
    # AWS/boto extra is not installed

logger = logging.getLogger(__name__)

# These are error codes we expect from AWS if we are making requests too fast.
# https://github.com/boto/botocore/blob/49f87350d54f55b687969ec8bf204df785975077/botocore/retries/standard.py#L316
THROTTLED_ERROR_CODES = [
        'Throttling',
        'ThrottlingException',
        'ThrottledException',
        'RequestThrottledException',
        'TooManyRequestsException',
        'ProvisionedThroughputExceededException',
        'TransactionInProgressException',
        'RequestLimitExceeded',
        'BandwidthLimitExceeded',
        'LimitExceededException',
        'RequestThrottled',
        'SlowDown',
        'PriorRequestNotComplete',
        'EC2ThrottledException',
]

@retry(errors=[BotoServerError])
def delete_iam_role(
    role_name: str, region: Optional[str] = None, quiet: bool = True
) -> None:
    from boto.iam.connection import IAMConnection
    # TODO: the Boto3 type hints are a bit oversealous here; they want hundreds
    # of overloads of the client-getting methods to exist based on the literal
    # string passed in, to return exactly the right kind of client or resource.
    # So we end up having to wrap all the calls in casts, which kind of defeats
    # the point of a nice fluent method you can call with the name of the thing
    # you want; we should have been calling iam_client() and so on all along if
    # we wanted MyPy to be able to understand us. So at some point we should
    # consider revising our API here to be less annoying to explain to the type
    # checker.
    iam_client = cast(IAMClient, session.client('iam', region_name=region))
    iam_resource = cast(IAMServiceResource, session.resource('iam', region_name=region))
    boto_iam_connection = IAMConnection()
    role = iam_resource.Role(role_name)
    # normal policies
    for attached_policy in role.attached_policies.all():
        printq(f'Now dissociating policy: {attached_policy.policy_name} from role {role.name}', quiet)
        role.detach_policy(PolicyArn=attached_policy.arn)
    # inline policies
    for inline_policy in role.policies.all():
        printq(f'Deleting inline policy: {inline_policy.policy_name} from role {role.name}', quiet)
        # couldn't find an easy way to remove inline policies with boto3; use boto
        boto_iam_connection.delete_role_policy(role.name, inline_policy.policy_name)
    iam_client.delete_role(RoleName=role_name)
    printq(f'Role {role_name} successfully deleted.', quiet)


@retry(errors=[BotoServerError])
def delete_iam_instance_profile(
    instance_profile_name: str, region: Optional[str] = None, quiet: bool = True
) -> None:
    iam_resource = cast(IAMServiceResource, session.resource("iam", region_name=region))
    instance_profile = iam_resource.InstanceProfile(instance_profile_name)
    if instance_profile.roles is not None:
        for role in instance_profile.roles:
            printq(f'Now dissociating role: {role.name} from instance profile {instance_profile_name}', quiet)
            instance_profile.remove_role(RoleName=role.name)
    instance_profile.delete()
    printq(f'Instance profile "{instance_profile_name}" successfully deleted.', quiet)


@retry(errors=[BotoServerError])
def delete_sdb_domain(
    sdb_domain_name: str, region: Optional[str] = None, quiet: bool = True
) -> None:
    sdb_client = cast(SimpleDBClient, session.client("sdb", region_name=region))
    sdb_client.delete_domain(DomainName=sdb_domain_name)
    printq(f'SBD Domain: "{sdb_domain_name}" successfully deleted.', quiet)


def connection_reset(e: Exception) -> bool:
    """
    Return true if an error is a connection reset error.
    """
    # For some reason we get 'error: [Errno 104] Connection reset by peer' where the
    # English description suggests that errno is 54 (ECONNRESET) while the actual
    # errno is listed as 104. To be safe, we check for both:
    return isinstance(e, socket.error) and e.errno in (errno.ECONNRESET, 104)

# TODO: Replace with: @retry and ErrorCondition
def retryable_s3_errors(e: Exception) -> bool:
    """
    Return true if this is an error from S3 that looks like we ought to retry our request.
    """
    return (connection_reset(e)
            or (isinstance(e, BotoServerError) and e.status in (429, 500))
            or (isinstance(e, BotoServerError) and e.code in THROTTLED_ERROR_CODES)
            # boto3 errors
            or (isinstance(e, (S3ResponseError, ClientError)) and get_error_code(e) in THROTTLED_ERROR_CODES)
            or (isinstance(e, ClientError) and 'BucketNotEmpty' in str(e))
            or (isinstance(e, ClientError) and e.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 409 and 'try again' in str(e))
            or (isinstance(e, ClientError) and e.response.get('ResponseMetadata', {}).get('HTTPStatusCode') in (404, 429, 500, 502, 503, 504)))


def retry_s3(delays: Iterable[float] = DEFAULT_DELAYS, timeout: float = DEFAULT_TIMEOUT, predicate: Callable[[Exception], bool] = retryable_s3_errors) -> Iterator[ContextManager[None]]:
    """
    Retry iterator of context managers specifically for S3 operations.
    """
    return old_retry(delays=delays, timeout=timeout, predicate=predicate)

@retry(errors=[BotoServerError])
def delete_s3_bucket(
    s3_resource: "S3ServiceResource",
    bucket: str,
    quiet: bool = True
) -> None:
    """
    Delete the given S3 bucket.
    """
    printq(f'Deleting s3 bucket: {bucket}', quiet)

    paginator = s3_resource.meta.client.get_paginator('list_object_versions')
    try:
        for response in paginator.paginate(Bucket=bucket):
            # Versions and delete markers can both go in here to be deleted.
            # They both have Key and VersionId, but there's no shared base type
            # defined for them in the stubs to express that. See
            # <https://github.com/vemel/mypy_boto3_builder/issues/123>. So we
            # have to do gymnastics to get them into the same list.
            to_delete: List[Dict[str, Any]] = cast(List[Dict[str, Any]], response.get('Versions', [])) + \
                                              cast(List[Dict[str, Any]], response.get('DeleteMarkers', []))
            for entry in to_delete:
                printq(f"    Deleting {entry['Key']} version {entry['VersionId']}", quiet)
                s3_resource.meta.client.delete_object(Bucket=bucket, Key=entry['Key'], VersionId=entry['VersionId'])
        s3_resource.Bucket(bucket).delete()
        printq(f'\n * Deleted s3 bucket successfully: {bucket}\n\n', quiet)
    except s3_resource.meta.client.exceptions.NoSuchBucket:
        printq(f'\n * S3 bucket no longer exists: {bucket}\n\n', quiet)


def create_s3_bucket(
    s3_resource: "S3ServiceResource",
    bucket_name: str,
    region: Union["BucketLocationConstraintType", Literal["us-east-1"]],
) -> "Bucket":
    """
    Create an AWS S3 bucket, using the given Boto3 S3 session, with the
    given name, in the given region.

    Supports the us-east-1 region, where bucket creation is special.

    *ALL* S3 bucket creation should use this function.
    """
    logger.debug("Creating bucket '%s' in region %s.", bucket_name, region)
    if region == "us-east-1":  # see https://github.com/boto/boto3/issues/125
        bucket = s3_resource.create_bucket(Bucket=bucket_name)
    else:
        bucket = s3_resource.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    return bucket

def get_bucket_region(bucket_name: str, endpoint_url: Optional[str] = None) -> str:
    """
    Get the AWS region name associated with the given S3 bucket.
    
    Takes an optional S3 API URL override.
    """
    s3_client = cast(S3Client, session.client('s3', endpoint_url=endpoint_url))
    for attempt in retry_s3():
        with attempt:
            loc = s3_client.get_bucket_location(Bucket=bucket_name)
            return bucket_location_to_region(loc.get('LocationConstraint', None))
            
def region_to_bucket_location(region: str) -> str:
    return '' if region == 'us-east-1' else region

def bucket_location_to_region(location: Optional[str]) -> str:
    return "us-east-1" if location == "" or location is None else location

def get_object_for_url(url: ParseResult, existing: Optional[bool] = None) -> Object:
        """
        Extracts a key (object) from a given parsed s3:// URL.

        :param bool existing: If True, key is expected to exist. If False, key is expected not to
               exists and it will be created. If None, the key will be created if it doesn't exist.
        """
        s3_resource = cast(S3ServiceResource, session.resource('s3'))

        keyName = url.path[1:]
        bucketName = url.netloc
        
        # Decide if we need to override Boto's built-in URL here.
        endpoint_url: Optional[str] = None
        host = os.environ.get('TOIL_S3_HOST', None)
        port = os.environ.get('TOIL_S3_PORT', None)
        protocol = 'https'
        if os.environ.get('TOIL_S3_USE_SSL', True) == 'False':
            protocol = 'http'
        if host:
            endpoint_url = f'{protocol}://{host}' + f':{port}' if port else ''

        # TODO: OrdinaryCallingFormat equivalent in boto3?
        # if botoargs:
        #     botoargs['calling_format'] = boto.s3.connection.OrdinaryCallingFormat()

        # Get the bucket's region to avoid a redirect per request
        region = get_bucket_region(bucketName, endpoint_url=endpoint_url)
        s3 = cast(S3ServiceResource, session.resource('s3', region_name=region, endpoint_url=endpoint_url))
        obj = s3.Object(bucketName, keyName)
        objExists = True

        try:
            obj.load()
        except ClientError as e:
            if get_error_status(e) == 404:
                objExists = False
            else:
                raise
        if existing is True and not objExists:
            raise RuntimeError(f"Key '{keyName}' does not exist in bucket '{bucketName}'.")
        elif existing is False and objExists:
            raise RuntimeError(f"Key '{keyName}' exists in bucket '{bucketName}'.")

        if not objExists:
            obj.put()  # write an empty file
        return obj

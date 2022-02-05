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
"""
Batch system for running Toil workflows on Kubernetes.

Ony useful with network-based job stores, like AWSJobStore.

Within non-privileged Kubernetes containers, additional Docker containers
cannot yet be launched. That functionality will need to wait for user-mode
Docker
"""
import datetime
import getpass
import logging
import os
import string
import subprocess
import sys
import tempfile
import time
import uuid
import yaml
from argparse import ArgumentParser, _ArgumentGroup
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

import kubernetes
import urllib3
from kubernetes.client.rest import ApiException

from toil import applianceSelf
from toil.batchSystems.abstractBatchSystem import (EXIT_STATUS_UNAVAILABLE_VALUE,
                                                   BatchJobExitReason,
                                                   UpdatedBatchJobInfo)
from toil.batchSystems.cleanup_support import BatchSystemCleanupSupport
from toil.batchSystems.contained_executor import pack_job
from toil.common import Toil
from toil.job import JobDescription
from toil.lib.conversions import human2bytes
from toil.lib.misc import slow_down, utc_now
from toil.lib.retry import ErrorCondition, retry
from toil.resource import Resource
from toil.statsAndLogging import configure_root_logger, set_log_level

logger = logging.getLogger(__name__)
retryable_kubernetes_errors = [urllib3.exceptions.MaxRetryError,
                               urllib3.exceptions.ProtocolError,
                               ApiException]


def is_retryable_kubernetes_error(e):
    """
    A function that determines whether or not Toil should retry or stop given
    exceptions thrown by Kubernetes.
    """
    for error in retryable_kubernetes_errors:
        if isinstance(e, error):
            return True
    return False


class KubernetesBatchSystem(BatchSystemCleanupSupport):
    @classmethod
    def supportsAutoDeployment(cls):
        return True

    def __init__(self, config, maxCores, maxMemory, maxDisk):
        super().__init__(config, maxCores, maxMemory, maxDisk)

        # Turn down log level for Kubernetes modules and dependencies.
        # Otherwise if we are at debug log level, we dump every
        # request/response to Kubernetes, including tokens which we shouldn't
        # reveal on CI.
        logging.getLogger('kubernetes').setLevel(logging.ERROR)
        logging.getLogger('requests_oauthlib').setLevel(logging.ERROR)

        # This will hold the last time our Kubernetes credentials were refreshed
        self.credential_time = None
        # And this will hold our cache of API objects
        self._apis = {}

        # Get our namespace (and our Kubernetes credentials to make sure they exist)
        self.namespace = self._api('namespace')

        # Decide if we are going to mount a Kubernetes host path as the Toil
        # work dir in the workers, for shared caching.
        self.host_path = config.kubernetes_host_path

        # Get the service account name to use, if any.
        self.service_account = config.kubernetes_service_account

        # Get the username to mark jobs with
        username = config.kubernetes_owner
        # And a unique ID for the run
        self.unique_id = uuid.uuid4()

        # Create a prefix for jobs, starting with our username
        self.job_prefix = f'{username}-toil-{self.unique_id}-'
        # Instead of letting Kubernetes assign unique job names, we assign our
        # own based on a numerical job ID. This functionality is managed by the
        # BatchSystemLocalSupport.

        # The UCSC GI Kubernetes cluster (and maybe other clusters?) appears to
        # relatively promptly destroy finished jobs for you if they don't
        # specify a ttlSecondsAfterFinished. This leads to "lost" Toil jobs,
        # and (if your workflow isn't allowed to run longer than the default
        # lost job recovery interval) timeouts of e.g. CWL Kubernetes
        # conformance tests. To work around this, we tag all our jobs with an
        # explicit TTL that is long enough that we're sure we can deal with all
        # the finished jobs before they expire.
        self.finished_job_ttl = 3600  # seconds

        # Here is where we will store the user script resource object if we get one.
        self.user_script: Optional[Resource] = None

        # Ge the image to deploy from Toil's configuration
        self.docker_image = applianceSelf()

        # Try and guess what Toil work dir the workers will use.
        # We need to be able to provision (possibly shared) space there.
        self.worker_work_dir = Toil.getToilWorkDir(config.workDir)
        if (config.workDir is None and
            os.getenv('TOIL_WORKDIR') is None and
            self.worker_work_dir == tempfile.gettempdir()):

            # We defaulted to the system temp directory. But we think the
            # worker Dockerfiles will make them use /var/lib/toil instead.
            # TODO: Keep this in sync with the Dockerfile.
            self.worker_work_dir = '/var/lib/toil'

        # Get the name of the AWS secret, if any, to mount in containers.
        self.aws_secret_name = os.environ.get("TOIL_AWS_SECRET_NAME", None)

        # Set this to True to enable the experimental wait-for-job-update code
        self.enable_watching = os.environ.get("KUBE_WATCH_ENABLED", False)

        # This will be a label to select all our jobs.
        self.run_id = f'toil-{self.unique_id}'

    def _pretty_print(self, kubernetes_object: Any) -> str:
        """
        Pretty-print a Kubernetes API object to a YAML string. Recursively
        drops boring fields.
        Takes any Kubernetes API object; not clear if any base type exists for
        them.
        """

        if not kubernetes_object:
            return 'None'

        # We need a Kubernetes widget that knows how to translate
        # its data structures to nice YAML-able dicts. See:
        # <https://github.com/kubernetes-client/python/issues/1117#issuecomment-939957007>
        api_client = kubernetes.client.ApiClient()

        # Convert to a dict
        root_dict = api_client.sanitize_for_serialization(kubernetes_object)

        def drop_boring(here):
            """
            Drop boring fields recursively.
            """
            boring_keys = []
            for k, v in here.items():
                if isinstance(v, dict):
                    drop_boring(v)
                if k in ['managedFields']:
                    boring_keys.append(k)
            for k in boring_keys:
                del here[k]

        drop_boring(root_dict)
        return yaml.dump(root_dict)

    def _api(self, kind, max_age_seconds = 5 * 60):
        """
        The Kubernetes module isn't clever enough to renew its credentials when
        they are about to expire. See
        https://github.com/kubernetes-client/python/issues/741.

        We work around this by making sure that every time we are about to talk
        to Kubernetes, we have fresh credentials. And we do that by reloading
        the config and replacing our Kubernetes API objects before we do any
        Kubernetes things.

        TODO: We can still get in trouble if a single watch or listing loop
        goes on longer than our credentials last, though.

        This method is the Right Way to get any Kubernetes API. You call it
        with the API you want ('batch', 'core', or 'customObjects') and it
        returns an API object with guaranteed fresh credentials.

        It also recognizes 'namespace' and returns our namespace as a string.

        max_age_seconds needs to be << your cluster's credential expiry time.
        """

        now = utc_now()

        if self.credential_time is None or (now - self.credential_time).total_seconds() > max_age_seconds:
            # Credentials need a refresh
            try:
                # Load ~/.kube/config or KUBECONFIG
                kubernetes.config.load_kube_config()
                # Worked. We're using kube config
                config_source = 'kube'
            except kubernetes.config.ConfigException:
                # Didn't work. Try pod-based credentials in case we are in a pod.
                try:
                    kubernetes.config.load_incluster_config()
                    # Worked. We're using in_cluster config
                    config_source = 'in_cluster'
                except kubernetes.config.ConfigException:
                    raise RuntimeError('Could not load Kubernetes configuration from ~/.kube/config, $KUBECONFIG, or current pod.')

            # Now fill in the API objects with these credentials
            self._apis['batch'] = kubernetes.client.BatchV1Api()
            self._apis['core'] = kubernetes.client.CoreV1Api()
            self._apis['customObjects'] = kubernetes.client.CustomObjectsApi()

            # And save the time
            self.credential_time = now
        if kind == 'namespace':
            # We just need the namespace string
            if config_source == 'in_cluster':
                # Our namespace comes from a particular file.
                with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as fh:
                    return fh.read().strip()
            else:
                # Find all contexts and the active context.
                # The active context gets us our namespace.
                contexts, activeContext = kubernetes.config.list_kube_config_contexts()
                if not contexts:
                    raise RuntimeError("No Kubernetes contexts available in ~/.kube/config or $KUBECONFIG")

                # Identify the namespace to work in
                return activeContext.get('context', {}).get('namespace', 'default')

        else:
            # We need an API object
            try:
                return self._apis[kind]
            except KeyError:
                raise RuntimeError(f"Unknown Kubernetes API type: {kind}")

    @retry(errors=retryable_kubernetes_errors)
    def _try_kubernetes(self, method, *args, **kwargs):
        """
        Kubernetes API can end abruptly and fail when it could dynamically backoff and retry.

        For example, calling self._api('batch').create_namespaced_job(self.namespace, job),
        Kubernetes can behave inconsistently and fail given a large job. See
        https://github.com/DataBiosphere/toil/issues/2884.

        This function gives Kubernetes more time to try an executable api.
        """
        return method(*args, **kwargs)

    @retry(errors=retryable_kubernetes_errors + [
               ErrorCondition(
                   error=ApiException,
                   error_codes=[404],
                   retry_on_this_condition=False
               )])
    def _try_kubernetes_expecting_gone(self, method, *args, **kwargs):
        """
        Same as _try_kubernetes, but raises 404 errors as soon as they are
        encountered (because we are waiting for them) instead of retrying on
        them.
        """
        return method(*args, **kwargs)

    def _try_kubernetes_stream(self, method, *args, **kwargs):
        """
        Kubernetes kubernetes.watch.Watch().stream() streams can fail and raise
        errors. We don't want to have those errors fail the entire workflow, so
        we handle them here.

        When you want to stream the results of a Kubernetes API method, call
        this instead of stream().

        To avoid having to do our own timeout logic, we finish the watch early
        if it produces an error.
        """

        w = kubernetes.watch.Watch()

        # We will set this to bypass our second catch in the case of user errors.
        userError = False

        try:
            for item in w.stream(method, *args, **kwargs):
                # For everything the watch stream gives us
                try:
                    # Show the item to user code
                    yield item
                except Exception as e:
                    # If we get an error from user code, skip our catch around
                    # the Kubernetes generator.
                    userError = True
                    raise
        except Exception as e:
            # If we get an error
            if userError:
                # It wasn't from the Kubernetes watch generator. Pass it along.
                raise
            else:
                # It was from the Kubernetes watch generator we manage.
                if is_retryable_kubernetes_error(e):
                    # This is just cloud weather.
                    # TODO: We will also get an APIError if we just can't code good against Kubernetes. So make sure to warn.
                    logger.warning("Received error from Kubernetes watch stream: %s", e)
                    # Just end the watch.
                    return
                else:
                    # Something actually weird is happening.
                    raise


    def setUserScript(self, userScript):
        logger.info(f'Setting user script for deployment: {userScript}')
        self.user_script = userScript

    # setEnv is provided by BatchSystemSupport, updates self.environment

    def _create_affinity(self, preemptable: bool) -> kubernetes.client.V1Affinity:
        """
        Make a V1Affinity that places pods appropriately depending on if they
        tolerate preemptable nodes or not.
        """

        # Describe preemptable nodes

        # There's no labeling standard for knowing which nodes are
        # preemptable across different cloud providers/Kubernetes clusters,
        # so we use the labels that EKS uses. Toil-managed Kubernetes
        # clusters also use this label. If we come to support more kinds of
        # preemptable nodes, we will need to add more labels to avoid here.
        preemptable_label = "eks.amazonaws.com/capacityType"
        preemptable_value = "SPOT"

        non_spot = [kubernetes.client.V1NodeSelectorRequirement(key=preemptable_label,
                                                                operator='NotIn',
                                                                values=[preemptable_value])]
        unspecified = [kubernetes.client.V1NodeSelectorRequirement(key=preemptable_label,
                                                                   operator='DoesNotExist')]
        # These are OR'd
        node_selector_terms = [kubernetes.client.V1NodeSelectorTerm(match_expressions=non_spot),
                               kubernetes.client.V1NodeSelectorTerm(match_expressions=unspecified)]
        node_selector = kubernetes.client.V1NodeSelector(node_selector_terms=node_selector_terms)


        if preemptable:
            # We can put this job anywhere. But we would be smart to prefer
            # preemptable nodes first, if available, so we don't block any
            # non-preemptable jobs.
            node_preference = kubernetes.client.V1PreferredSchedulingTerm(weight=1, preference=node_selector)

            node_affinity = kubernetes.client.V1NodeAffinity(preferred_during_scheduling_ignored_during_execution=[node_preference])
        else:
            # We need to add some selector stuff to keep the job off of
            # nodes that might be preempted.
            node_affinity = kubernetes.client.V1NodeAffinity(required_during_scheduling_ignored_during_execution=node_selector)

        # Make the node affinity into an overall affinity
        return kubernetes.client.V1Affinity(node_affinity=node_affinity)

    def _create_pod_spec(
            self,
            job_desc: JobDescription,
            job_environment: Optional[Dict[str, str]] = None
    ) -> kubernetes.client.V1PodSpec:
        """
        Make the specification for a pod that can execute the given job.
        """

        environment = self.environment.copy()
        if job_environment:
            environment.update(job_environment)

        # Make a command to run it in the executor
        command_list = pack_job(job_desc, self.user_script, environment=environment)

        # The Kubernetes API makes sense only in terms of the YAML format. Objects
        # represent sections of the YAML files. Except from our point of view, all
        # the internal nodes in the YAML structure are named and typed.

        # For docs, start at the root of the job hierarchy:
        # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1Job.md

        # Make a definition for the container's resource requirements.
        # Add on a bit for Kubernetes overhead (Toil worker's memory, hot deployed
        # user scripts).
        # Kubernetes needs some lower limit of memory to run the pod at all without
        # OOMing. We also want to provision some extra space so that when
        # we test _isPodStuckOOM we never get True unless the job has
        # exceeded job_desc.memory.
        requirements_dict = {'cpu': job_desc.cores,
                             'memory': job_desc.memory + 1024 * 1024 * 512,
                             'ephemeral-storage': job_desc.disk + 1024 * 1024 * 512}
        # Use the requirements as the limits, for predictable behavior, and because
        # the UCSC Kubernetes admins want it that way.
        limits_dict = requirements_dict
        resources = kubernetes.client.V1ResourceRequirements(limits=limits_dict,
                                                             requests=requirements_dict)

        # Collect volumes and mounts
        volumes = []
        mounts = []

        if self.host_path is not None:
            # Provision Toil WorkDir from a HostPath volume, to share with other pods
            host_path_volume_name = 'workdir'
            # Use type='Directory' to fail if the host directory doesn't exist already.
            host_path_volume_source = kubernetes.client.V1HostPathVolumeSource(path=self.host_path, type='Directory')
            host_path_volume = kubernetes.client.V1Volume(name=host_path_volume_name,
                                                         host_path=host_path_volume_source)
            volumes.append(host_path_volume)
            host_path_volume_mount = kubernetes.client.V1VolumeMount(mount_path=self.worker_work_dir, name=host_path_volume_name)
            mounts.append(host_path_volume_mount)
        else:
            # Provision Toil WorkDir as an ephemeral volume
            ephemeral_volume_name = 'workdir'
            ephemeral_volume_source = kubernetes.client.V1EmptyDirVolumeSource()
            ephemeral_volume = kubernetes.client.V1Volume(name=ephemeral_volume_name,
                                                          empty_dir=ephemeral_volume_source)
            volumes.append(ephemeral_volume)
            ephemeral_volume_mount = kubernetes.client.V1VolumeMount(mount_path=self.worker_work_dir, name=ephemeral_volume_name)
            mounts.append(ephemeral_volume_mount)

        if self.aws_secret_name is not None:
            # Also mount an AWS secret, if provided.
            # TODO: make this generic somehow
            secret_volume_name = 's3-credentials'
            secret_volume_source = kubernetes.client.V1SecretVolumeSource(secret_name=self.aws_secret_name)
            secret_volume = kubernetes.client.V1Volume(name=secret_volume_name,
                                                       secret=secret_volume_source)
            volumes.append(secret_volume)
            secret_volume_mount = kubernetes.client.V1VolumeMount(mount_path='/root/.aws', name=secret_volume_name)
            mounts.append(secret_volume_mount)

        # Make a container definition
        container = kubernetes.client.V1Container(command=command_list,
                                                  image=self.docker_image,
                                                  name="runner-container",
                                                  resources=resources,
                                                  volume_mounts=mounts)
        # Wrap the container in a spec
        pod_spec = kubernetes.client.V1PodSpec(containers=[container],
                                               volumes=volumes,
                                               restart_policy="Never")
        # Tell the spec where to land
        pod_spec.affinity = self._create_affinity(job_desc.preemptable)

        if self.service_account:
            # Apply service account if set
            pod_spec.service_account_name = self.service_account

        return pod_spec

    def issueBatchJob(self, job_desc, job_environment: Optional[Dict[str, str]] = None):
        # TODO: get a sensible self.maxCores, etc. so we can checkResourceRequest.
        # How do we know if the cluster will autoscale?

        # Try the job as local
        localID = self.handleLocalJob(job_desc)
        if localID is not None:
            # It is a local job
            return localID
        else:
            # We actually want to send to the cluster

            # Check resource requirements (managed by BatchSystemSupport)
            self.checkResourceRequest(job_desc.memory, job_desc.cores, job_desc.disk)

            # Make a pod that describes running the job
            pod_spec = self._create_pod_spec(job_desc, job_environment=job_environment)

            # Make a batch system scope job ID
            jobID = self.getNextJobID()
            # Make a unique name
            jobName = self.job_prefix + str(jobID)

            # Make metadata to label the job/pod with info.
            # Don't let the cluster autoscaler evict any Toil jobs.
            metadata = kubernetes.client.V1ObjectMeta(name=jobName,
                                                      labels={"toil_run": self.run_id},
                                                      annotations={"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"})

            # Wrap the spec in a template
            template = kubernetes.client.V1PodTemplateSpec(spec=pod_spec, metadata=metadata)

            # Make another spec for the job, asking to run the template with no
            # backoff/retry. Specify our own TTL to avoid catching the notice
            # of over-zealous abandoned job cleanup scripts.
            job_spec = kubernetes.client.V1JobSpec(template=template,
                                                   backoff_limit=0,
                                                   ttl_seconds_after_finished=self.finished_job_ttl)

            # And make the actual job
            job = kubernetes.client.V1Job(spec=job_spec,
                                          metadata=metadata,
                                          api_version="batch/v1",
                                          kind="Job")

            # Make the job
            launched = self._try_kubernetes(self._api('batch').create_namespaced_job, self.namespace, job)

            logger.debug('Launched job: %s', jobName)

            return jobID

    def _ourJobObject(self, onlySucceeded=False):
        """
        Yield Kubernetes V1Job objects that we are responsible for that the
        cluster knows about.

        Doesn't support a free-form selector, because there's only about 3
        things jobs can be selected on: https://stackoverflow.com/a/55808444

        :param bool onlySucceeded: restrict results to succeeded jobs.
        :param int limit: max results to yield.
        """

        # We need to page through the list from the cluster with a continuation
        # token. These expire after about 5 minutes. If we use an expired one,
        # we get a 410 error and a new token, and we can use the new token to
        # get the rest of the list, but the list will be updated.
        #
        # TODO: How to get the new token isn't clear. See
        # https://github.com/kubernetes-client/python/issues/953. For now we
        # will just throw an error if we don't get to the end of the list in
        # time.

        token = None

        while True:
            # We can't just pass e.g. a None continue token when there isn't
            # one, because the Kubernetes module reads its kwargs dict and
            # cares about presence/absence. So we build a dict to send.
            kwargs = {}

            if token is not None:
                kwargs['_continue'] = token

            if onlySucceeded:
                results =  self._try_kubernetes(self._api('batch').list_namespaced_job, self.namespace,
                                                label_selector=f"toil_run={self.run_id}", field_selector="status.successful==1", **kwargs)
            else:
                results = self._try_kubernetes(self._api('batch').list_namespaced_job, self.namespace,
                                                label_selector=f"toil_run={self.run_id}", **kwargs)
            yield from results.items  # These jobs belong to us

            # Remember the continuation token, if any
            token = getattr(results.metadata, 'continue', None)

            if token is None:
                # There isn't one. We got everything.
                break


    def _ourPodObject(self):
        """
        Yield Kubernetes V1Pod objects that we are responsible for that the
        cluster knows about.
        """

        token = None

        while True:
            # We can't just pass e.g. a None continue token when there isn't
            # one, because the Kubernetes module reads its kwargs dict and
            # cares about presence/absence. So we build a dict to send.
            kwargs = {}

            if token is not None:
                kwargs['_continue'] = token

            results = self._try_kubernetes(self._api('core').list_namespaced_pod, self.namespace, label_selector=f"toil_run={self.run_id}", **kwargs)

            yield from results.items
            # Remember the continuation token, if any
            token = getattr(results.metadata, 'continue', None)

            if token is None:
                # There isn't one. We got everything.
                break


    def _getPodForJob(self, jobObject):
        """
        Get the pod that belongs to the given job, or None if the job's pod is
        missing. The pod knows about things like the job's exit code.

        :param kubernetes.client.V1Job jobObject: a Kubernetes job to look up
                                       pods for.

        :return: The pod for the job, or None if no pod is found.
        :rtype: kubernetes.client.V1Pod
        """

        token = None

        # Work out what the return code was (which we need to get from the
        # pods) We get the associated pods by querying on the label selector
        # `job-name=JOBNAME`
        query = f'job-name={jobObject.metadata.name}'

        while True:
            # We can't just pass e.g. a None continue token when there isn't
            # one, because the Kubernetes module reads its kwargs dict and
            # cares about presence/absence. So we build a dict to send.
            kwargs = {'label_selector': query}
            if token is not None:
                kwargs['_continue'] = token
            results = self._try_kubernetes(self._api('core').list_namespaced_pod, self.namespace, **kwargs)

            for pod in results.items:
                # Return the first pod we find
                return pod

            # Remember the continuation token, if any
            token = getattr(results.metadata, 'continue', None)

            if token is None:
                # There isn't one. We got everything.
                break

        # If we get here, no pages had any pods.
        return None

    def _getLogForPod(self, podObject):
        """
        Get the log for a pod.

        :param kubernetes.client.V1Pod podObject: a Kubernetes pod with one
                                       container to get the log from.

        :return: The log for the only container in the pod.
        :rtype: str

        """

        return self._try_kubernetes(self._api('core').read_namespaced_pod_log, podObject.metadata.name,
                                                         namespace=self.namespace)

    def _isPodStuckOOM(self, podObject, minFreeBytes=1024 * 1024 * 2):
        """
        Poll the current memory usage for the pod from the cluster.

        Return True if the pod looks to be in a soft/stuck out of memory (OOM)
        state, where it is using too much memory to actually make progress, but
        not enough to actually trigger the OOM killer to kill it. For some
        large memory limits, on some Kubernetes clusters, pods can get stuck in
        this state when their memory limits are high (approx. 200 Gi).

        We operationalize "OOM" as having fewer than minFreeBytes bytes free.

        We assume the pod has only one container, as Toil's pods do.

        If the metrics service is not working, we treat the pod as not being
        stuck OOM. Otherwise, we would kill all functioning jobs on clusters
        where the metrics service is down or isn't installed.

        :param kubernetes.client.V1Pod podObject: a Kubernetes pod with one
                                       container to check up on.
        :param int minFreeBytes: Minimum free bytes to not be OOM.

        :return: True if the pod is OOM, False otherwise.
        :rtype: bool
        """

        # Compose a query to get just the pod we care about
        query = 'metadata.name=' + podObject.metadata.name

        # Look for it, but manage our own exceptions
        try:
            # TODO: When the Kubernetes Python API actually wraps the metrics API, switch to that
            response = self._api('customObjects').list_namespaced_custom_object('metrics.k8s.io', 'v1beta1',
                                                                                self.namespace, 'pods',
                                                                                field_selector=query)
        except Exception as e:
            # We couldn't talk to the metrics service on this attempt. We don't
            # retry, but we also don't want to just ignore all errors. We only
            # want to ignore errors we expect to see if the problem is that the
            # metrics service is not working.
            if type(e) in retryable_kubernetes_errors:
                # This is the sort of error we would expect from an overloaded
                # Kubernetes or a dead metrics service.
                # We can't tell that the pod is stuck, so say that it isn't.
                logger.warning("Could not query metrics service: %s", e)
                return False
            else:
                raise

        # Pull out the items
        items = response.get('items', [])

        if len(items) == 0:
            # If there's no statistics we can't say we're stuck OOM
            return False

        # Assume the first result is the right one, because of the selector
        # Assume it has exactly one pod, because we made it
        containers = items[0].get('containers', [{}])

        if len(containers) == 0:
            # If there are no containers (because none have started yet?), we can't say we're stuck OOM
            return False

        # Otherwise, assume it just has one container.
        # Grab the memory usage string, like 123Ki, and convert to bytes.
        # If anything is missing, assume 0 bytes used.
        bytesUsed = human2bytes(containers[0].get('usage', {}).get('memory', '0'))

        # Also get the limit out of the pod object's spec
        bytesAllowed = human2bytes(podObject.spec.containers[0].resources.limits['memory'])

        if bytesAllowed - bytesUsed < minFreeBytes:
            # This is too much!
            logger.warning('Pod %s has used %d of %d bytes of memory; reporting as stuck due to OOM.',
                           podObject.metadata.name, bytesUsed, bytesAllowed)

            return True




    def _getIDForOurJob(self, jobObject):
        """
        Get the JobID number that belongs to the given job that we own.

        :param kubernetes.client.V1Job jobObject: a Kubernetes job object that is a job we issued.

        :return: The JobID for the job.
        :rtype: int
        """

        return int(jobObject.metadata.name[len(self.job_prefix):])


    def getUpdatedBatchJob(self, maxWait):

        entry = datetime.datetime.now()

        result = self._getUpdatedBatchJobImmediately()

        if result is not None or maxWait == 0:
            # We got something on the first try, or we only get one try
            return result

        # Otherwise we need to maybe wait.
        if self.enable_watching:
            for event in self._try_kubernetes_stream(self._api('batch').list_namespaced_job, self.namespace,
                                                        label_selector=f"toil_run={self.run_id}",
                                                        timeout_seconds=maxWait):
                # Grab the metadata data, ID, the list of conditions of the current job, and the total pods
                jobObject = event['object']
                jobID = int(jobObject.metadata.name[len(self.job_prefix):])
                jobObjectListConditions =jobObject.status.conditions
                totalPods = jobObject.status.active + jobObject.status.finished + jobObject.status.failed
                # Exit Reason defaults to 'Successfully Finished` unless said otherwise
                exitReason = BatchJobExitReason.FINISHED
                exitCode = 0

                # Check if there are any active pods
                if jobObject.status.acitve > 0:
                    logger.info("%s has %d pods running" % jobObject.metadata.name, jobObject.status.active)
                    continue
                elif jobObject.status.failed > 0 or jobObject.status.finished > 0:
                    # No more active pods in the current job ; must be finished
                    logger.info("%s RESULTS -> Succeeded: %d Failed:%d Active:%d" % jobObject.metadata.name,
                                                                jobObject.status.succeeded, jobObject.status.failed, jobObject.status.active)
                    # Get termination information of job
                    termination = jobObjectListConditions[0]
                    # Log out success/failure given a reason
                    logger.info("%s REASON: %s", termination.type, termination.reason)

                    # Log out reason of failure and pod exit code
                    if jobObject.status.failed > 0:
                        exitReason = BatchJobExitReason.FAILED
                        pod = self._getPodForJob(jobObject)
                        logger.debug("Failed job %s", self._pretty_print(jobObject))
                        logger.warning("Failed Job Message: %s", termination.message)
                        exitCode = pod.status.container_statuses[0].state.terminated.exit_code

                    runtime = slow_down((termination.completion_time - termination.start_time).total_seconds())
                    result = UpdatedBatchJobInfo(jobID=jobID, exitStatus=exitCode, wallTime=runtime, exitReason=exitReason)

                    if (exitReason == BatchJobExitReason.FAILED) or (jobObject.status.finished == totalPods):
                        # Cleanup if job is all finished or there was a pod that failed
                        logger.debug('Deleting Kubernetes job %s', jobObject.metadata.name)
                        self._try_kubernetes(self._api('batch').delete_namespaced_job,
                                            jobObject.metadata.name,
                                            self.namespace,
                                            propagation_policy='Foreground')
                        self._waitForJobDeath(jobObject.metadata.name)
                        return result
                    continue
                else:
                    # Job is not running/updating ; no active, successful, or failed pods yet
                    logger.debug("Job {} -> {}".format(jobObject.metadata.name, jobObjectListConditions[0].reason))
                    # Pod could be pending; don't say it's lost.
                    continue
        else:
            # Try polling instead
            while result is None and (datetime.datetime.now() - entry).total_seconds() < maxWait:
                # We still have nothing and we haven't hit the timeout.

                # Poll
                result = self._getUpdatedBatchJobImmediately()

                if result is None:
                    # Still nothing. Wait a second, or some fraction of our max wait time.
                    time.sleep(min(maxWait/2, 1.0))

            # When we get here, either we found something or we ran out of time
            return result


    def _getUpdatedBatchJobImmediately(self):
        """
        Return None if no updated (completed or failed) batch job is currently
        available, and jobID, exitCode, runtime if such a job can be found.
        """

        # See if a local batch job has updated and is available immediately
        local_tuple = self.getUpdatedLocalJob(0)
        if local_tuple:
            # If so, use it
            return local_tuple

        # Otherwise we didn't get a local job.

        # Go looking for other jobs

        # Everybody else does this with a queue and some other thread that
        # is responsible for populating it.
        # But we can just ask kubernetes now.

        # Find a job that is done, failed, or stuck
        jobObject = None
        # Put 'done', 'failed', or 'stuck' here
        chosenFor = ''

        for j in self._ourJobObject(onlySucceeded=True):
            # Look for succeeded jobs because that's the only filter Kubernetes has
            jobObject = j
            chosenFor = 'done'

        if jobObject is None:
            for j in self._ourJobObject():
                # If there aren't any succeeded jobs, scan all jobs
                # See how many times each failed
                failCount = getattr(j.status, 'failed', 0)
                if failCount is None:
                    # Make sure it is an int
                    failCount = 0
                if failCount > 0:
                    # Take the first failed one you find
                    jobObject = j
                    chosenFor = 'failed'
                    break

        if jobObject is None:
            # If no jobs are failed, look for jobs with pods that are stuck for various reasons.
            for j in self._ourJobObject():
                pod = self._getPodForJob(j)

                if pod is None:
                    # Skip jobs with no pod
                    continue

                # Containers can get stuck in Waiting with reason ImagePullBackOff

                # Get the statuses of the pod's containers
                containerStatuses = pod.status.container_statuses
                if containerStatuses is None or len(containerStatuses) == 0:
                    # Pod exists but has no container statuses
                    # This happens when the pod is just "Scheduled"
                    # ("PodScheduled" status event) and isn't actually starting
                    # to run yet.
                    # Can't be stuck in ImagePullBackOff
                    continue

                waitingInfo = getattr(getattr(pod.status.container_statuses[0], 'state', None), 'waiting', None)
                if waitingInfo is not None and waitingInfo.reason == 'ImagePullBackOff':
                    # Assume it will never finish, even if the registry comes back or whatever.
                    # We can get into this state when we send in a non-existent image.
                    # See https://github.com/kubernetes/kubernetes/issues/58384
                    jobObject = j
                    chosenFor = 'stuck'
                    logger.warning('Failing stuck job; did you try to run a non-existent Docker image?'
                                   ' Check TOIL_APPLIANCE_SELF.')
                    break

                # Pods can also get stuck nearly but not quite out of memory,
                # if their memory limits are high and they try to exhaust them.

                if self._isPodStuckOOM(pod):
                    # We found a job that probably should be OOM! Report it as stuck.
                    # Polling function takes care of the logging.
                    jobObject = j
                    chosenFor = 'stuck'
                    break

        if jobObject is None:
            # Say we couldn't find anything
            return None
        else:
            # We actually have something
            logger.debug('Identified stopped Kubernetes job %s as %s', jobObject.metadata.name, chosenFor)


        # Otherwise we got something.

        # Work out what the job's ID was (whatever came after our name prefix)
        jobID = int(jobObject.metadata.name[len(self.job_prefix):])

        # Work out when the job was submitted. If the pod fails before actually
        # running, this is the basis for our runtime.
        jobSubmitTime = getattr(jobObject.status, 'start_time', None)
        if jobSubmitTime is None:
            # If somehow this is unset, say it was just now.
            jobSubmitTime = utc_now()

        # Grab the pod
        pod = self._getPodForJob(jobObject)

        if pod is not None:
            if chosenFor == 'done' or chosenFor == 'failed':
                # The job actually finished or failed

                # Get the statuses of the pod's containers
                containerStatuses = pod.status.container_statuses

                # Get when the pod started (reached the Kubelet) as a datetime
                startTime = getattr(pod.status, 'start_time', None)
                if startTime is None:
                    # If the pod never made it to the kubelet to get a
                    # start_time, say it was when the job was submitted.
                    startTime = jobSubmitTime

                if containerStatuses is None or len(containerStatuses) == 0:
                    # No statuses available.
                    # This happens when a pod is "Scheduled". But how could a
                    # 'done' or 'failed' pod be merely "Scheduled"?
                    # Complain so we can find out.
                    logger.warning('Exit code and runtime unavailable; pod has no container statuses')
                    logger.warning('Pod: %s', str(pod))
                    exitCode = EXIT_STATUS_UNAVAILABLE_VALUE
                    # Say it stopped now and started when it was scheduled/submitted.
                    # We still need a strictly positive runtime.
                    runtime = slow_down((utc_now() - startTime).total_seconds())
                else:
                    # Get the termination info from the pod's main (only) container
                    terminatedInfo = getattr(getattr(containerStatuses[0], 'state', None), 'terminated', None)
                    if terminatedInfo is None:
                        logger.warning('Exit code and runtime unavailable; pod stopped without container terminating')
                        logger.warning('Pod: %s', str(pod))
                        exitCode = EXIT_STATUS_UNAVAILABLE_VALUE
                        # Say it stopped now and started when it was scheduled/submitted.
                        # We still need a strictly positive runtime.
                        runtime = slow_down((utc_now() - startTime).total_seconds())
                    else:
                        # Extract the exit code
                        exitCode = terminatedInfo.exit_code

                        # Compute how long the job actually ran for (subtract
                        # datetimes). We need to look at the pod's start time
                        # because the job's start time is just when the job is
                        # created. And we need to look at the pod's end time
                        # because the job only gets a completion time if
                        # successful.
                        runtime = slow_down((terminatedInfo.finished_at -
                                             pod.status.start_time).total_seconds())

                        if chosenFor == 'failed':
                            # Warn the user with the failed pod's log
                            # TODO: cut this down somehow?
                            logger.warning('Log from failed pod: %s', self._getLogForPod(pod))

            else:
                # The job has gotten stuck

                assert chosenFor == 'stuck'

                # Synthesize an exit code
                exitCode = EXIT_STATUS_UNAVAILABLE_VALUE
                # Say it ran from when the job was submitted to when the pod got stuck
                runtime = slow_down((utc_now() - jobSubmitTime).total_seconds())
        else:
            # The pod went away from under the job.
            logging.warning('Exit code and runtime unavailable; pod vanished')
            exitCode = EXIT_STATUS_UNAVAILABLE_VALUE
            # Say it ran from when the job was submitted to when the pod vanished
            runtime = slow_down((utc_now() - jobSubmitTime).total_seconds())


        try:
            # Delete the job and all dependents (pods), hoping to get a 404 if it's magically gone
            logger.debug('Deleting Kubernetes job %s', jobObject.metadata.name)
            self._try_kubernetes_expecting_gone(self._api('batch').delete_namespaced_job, jobObject.metadata.name,
                                                self.namespace,
                                                propagation_policy='Foreground')

            # That just kicks off the deletion process. Foreground doesn't
            # actually block. See
            # https://kubernetes.io/docs/concepts/workloads/controllers/garbage-collection/#foreground-cascading-deletion
            # We have to either wait until the deletion is done and we can't
            # see the job anymore, or ban the job from being "updated" again if
            # we see it. If we don't block on deletion, we can't use limit=1
            # on our query for succeeded jobs. So we poll for the job's
            # non-existence.
            self._waitForJobDeath(jobObject.metadata.name)

        except ApiException as e:
            if e.status != 404:
                # Something is wrong, other than the job already being deleted.
                raise
            # Otherwise everything is fine and the job is gone.

        # Return the one finished job we found
        return UpdatedBatchJobInfo(jobID=jobID, exitStatus=exitCode, wallTime=runtime, exitReason=None)

    def _waitForJobDeath(self, jobName):
        """
        Block until the job with the given name no longer exists.
        """

        # We do some exponential backoff on the polling
        # TODO: use a wait instead of polling?
        backoffTime = 0.1
        maxBackoffTime = 6.4
        while True:
            try:
                # Look for the job
                self._try_kubernetes_expecting_gone(self._api('batch').read_namespaced_job, jobName, self.namespace)
                # If we didn't 404, wait a bit with exponential backoff
                time.sleep(backoffTime)
                if backoffTime < maxBackoffTime:
                    backoffTime *= 2
            except ApiException as e:
                # We finally got a failure!
                if e.status != 404:
                    # But it wasn't due to the job being gone; something is wrong.
                    raise
                # It was a 404; the job is gone. Stop polling it.
                break

    def shutdown(self) -> None:

        # Shutdown local processes first
        self.shutdownLocal()


        # Kill all of our jobs and clean up pods that are associated with those jobs
        try:
            logger.debug('Deleting all Kubernetes jobs for toil_run=%s', self.run_id)
            self._try_kubernetes_expecting_gone(self._api('batch').delete_collection_namespaced_job,
                                                            self.namespace,
                                                            label_selector=f"toil_run={self.run_id}",
                                                            propagation_policy='Background')
            logger.debug('Killed jobs with delete_collection_namespaced_job; cleaned up')
        except ApiException as e:
            if e.status != 404:
                # Anything other than a 404 is weird here.
                logger.error("Exception when calling BatchV1Api->delete_collection_namespaced_job: %s" % e)

            # aggregate all pods and check if any pod has failed to cleanup or is orphaned.
            ourPods = self._ourPodObject()

            for pod in ourPods:
                try:
                    if pod.status.phase == 'Failed':
                            logger.debug('Failed pod encountered at shutdown:\n%s', self._pretty_print(pod))
                    if pod.status.phase == 'Orphaned':
                            logger.debug('Orphaned pod encountered at shutdown:\n%s', self._pretty_print(pod))
                except:
                    # Don't get mad if that doesn't work.
                    pass
                try:
                    logger.debug('Cleaning up pod at shutdown: %s', pod.metadata.name)
                    respone = self._try_kubernetes_expecting_gone(self._api('core').delete_namespaced_pod,  pod.metadata.name,
                                        self.namespace,
                                        propagation_policy='Background')
                except ApiException as e:
                    if e.status != 404:
                        # Anything other than a 404 is weird here.
                        logger.error("Exception when calling CoreV1Api->delete_namespaced_pod: %s" % e)


    def _getIssuedNonLocalBatchJobIDs(self):
        """
        Get the issued batch job IDs that are not for local jobs.
        """
        jobIDs = []
        got_list = self._ourJobObject()
        for job in got_list:
            # Get the ID for each job
            jobIDs.append(self._getIDForOurJob(job))
        return jobIDs

    def getIssuedBatchJobIDs(self):
        # Make sure to send the local jobs also
        return self._getIssuedNonLocalBatchJobIDs() + list(self.getIssuedLocalJobIDs())

    def getRunningBatchJobIDs(self):
        # We need a dict from jobID (integer) to seconds it has been running
        secondsPerJob = dict()
        for job in self._ourJobObject():
            # Grab the pod for each job
            pod = self._getPodForJob(job)

            if pod is None:
                # Jobs whose pods are gone are not running
                continue

            if pod.status.phase == 'Running':
                # The job's pod is running

                # The only time we have handy is when the pod got assigned to a
                # kubelet, which is technically before it started running.
                runtime = (utc_now() - pod.status.start_time).total_seconds()

                # Save it under the stringified job ID
                secondsPerJob[self._getIDForOurJob(job)] = runtime
        # Mix in the local jobs
        secondsPerJob.update(self.getRunningLocalJobIDs())
        return secondsPerJob

    def killBatchJobs(self, jobIDs):

        # Kill all the ones that are local
        self.killLocalJobs(jobIDs)

        # Clears workflow's jobs listed in jobIDs.

        # First get the jobs we even issued non-locally
        issuedOnKubernetes = set(self._getIssuedNonLocalBatchJobIDs())

        for jobID in jobIDs:
            # For each job we are supposed to kill
            if jobID not in issuedOnKubernetes:
                # It never went to Kubernetes (or wasn't there when we just
                # looked), so we can't kill it on Kubernetes.
                continue
            # Work out what the job would be named
            jobName = self.job_prefix + str(jobID)

            # Delete the requested job in the foreground.
            # This doesn't block, but it does delete expeditiously.
            logger.debug('Deleting Kubernetes job %s', jobName)
            response = self._try_kubernetes(self._api('batch').delete_namespaced_job, jobName,
                                                                self.namespace,
                                                                propagation_policy='Foreground')
            logger.debug('Killed job by request: %s', jobName)

        for jobID in jobIDs:
            # Now we need to wait for all the jobs we killed to be gone.

            # Work out what the job would be named
            jobName = self.job_prefix + str(jobID)

            # Block until it doesn't exist
            self._waitForJobDeath(jobName)

    @classmethod
    def get_default_kubernetes_owner(cls) -> str:
        """
        Get the default Kubernetes-acceptable username string to tack onto jobs.
        """

        # Make a Kubernetes-acceptable version of our username: not too long,
        # and all lowercase letters, numbers, or - or .
        acceptable_chars = set(string.ascii_lowercase + string.digits + '-.')

        return ''.join([c for c in getpass.getuser().lower() if c in acceptable_chars])[:100]

    @classmethod
    def add_options(cls, parser: Union[ArgumentParser, _ArgumentGroup]) -> None:
        parser.add_argument("--kubernetesHostPath", dest="kubernetes_host_path", default=None,
                            help="Path on Kubernetes hosts to use as shared inter-pod temp directory.  "
                                 "(default: %(default)s)")
        parser.add_argument("--kubernetesOwner", dest="kubernetes_owner", default=cls.get_default_kubernetes_owner(),
                            help="Username to mark Kubernetes jobs with.  "
                                 "(default: %(default)s)")
        parser.add_argument("--kubernetesServiceAccount", dest="kubernetes_service_account", default=None,
                            help="Service account to run jobs as.  "
                                 "(default: %(default)s)")

    OptionType = TypeVar('OptionType')
    @classmethod
    def setOptions(cls, setOption: Callable[[str, Optional[Callable[[str], OptionType]], Optional[Callable[[OptionType], None]], Optional[OptionType], Optional[List[str]]], None]) -> None:
        setOption("kubernetes_host_path", default=None, env=['TOIL_KUBERNETES_HOST_PATH'])
        setOption("kubernetes_owner", default=cls.get_default_kubernetes_owner(), env=['TOIL_KUBERNETES_OWNER'])
        setOption("kubernetes_service_account", default=None, env=['TOIL_KUBERNETES_SERVICE_ACCOUNT'])


import contextlib
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError, WaiterError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cloudfront = boto3.client('cloudfront', config=Config(
    retries = {
        'max_attempts': 10,
        'mode': 'standard',
    }
))
# The archive-aware uploader can issue up to 32 concurrent object requests.
s3 = boto3.client('s3', config=Config(max_pool_connections=32))

CFN_SUCCESS = "SUCCESS"
CFN_FAILED = "FAILED"
ENV_KEY_MOUNT_PATH = "MOUNT_PATH"
ENV_KEY_SKIP_CLEANUP = "SKIP_CLEANUP"

AWS_CLI_CONFIG_FILE = "/tmp/aws_cli_config"
CUSTOM_RESOURCE_OWNER_TAG = "aws-cdk:cr-owned"

os.putenv('AWS_CONFIG_FILE', AWS_CLI_CONFIG_FILE)

def handler(event, context):

    def cfn_error(message=None):
        if message:
            logger.error("| cfn_error: %s" % sanitize_message(message))
        cfn_send(event, context, CFN_FAILED, reason=message, physicalResourceId=event.get('PhysicalResourceId', None))


    try:
        # We are not logging ResponseURL as this is a pre-signed S3 URL, and could be used to tamper
        # with the response CloudFormation sees from this Custom Resource execution.
        logger.info(sanitize_message({ key:value for (key, value) in event.items() if key != 'ResponseURL'}))

        # cloudformation request type (create/update/delete)
        request_type = event['RequestType']

        # extract resource properties
        props = event['ResourceProperties']
        old_props = event.get('OldResourceProperties', {})
        physical_id = event.get('PhysicalResourceId', None)

        try:
            source_bucket_names = props['SourceBucketNames']
            source_object_keys  = props['SourceObjectKeys']
            source_markers      = props.get('SourceMarkers', None)
            source_markers_config = props.get('SourceMarkersConfig', None)
            dest_bucket_name    = props['DestinationBucketName']
            dest_bucket_prefix  = props.get('DestinationBucketKeyPrefix', '')
            extract             = props.get('Extract', 'true') == 'true'
            retain_on_delete    = props.get('RetainOnDelete', "true") == "true"
            distribution_id     = props.get('DistributionId', '')
            wait_for_distribution_invalidation = props.get('WaitForDistributionInvalidation', True)
            user_metadata       = props.get('UserMetadata', {})
            system_metadata     = props.get('SystemMetadata', {})
            prune               = props.get('Prune', 'true').lower() == 'true'
            exclude             = props.get('Exclude', [])
            include             = props.get('Include', [])
            sign_content        = props.get('SignContent', 'false').lower() == 'true'
            output_object_keys  = props.get('OutputObjectKeys', 'true') == 'true'
            output_object_version_ids = props.get('OutputObjectVersionIds', 'false') == 'true'

            # backwards compatibility - if "SourceMarkers" is not specified,
            # assume all sources have an empty market map
            if source_markers is None:
                source_markers = [{} for i in range(len(source_bucket_names))]
            if source_markers_config is None:
                source_markers_config = [{} for i in range(len(source_bucket_names))]

            default_distribution_path = dest_bucket_prefix
            if not default_distribution_path.endswith("/"):
                default_distribution_path += "/"
            if not default_distribution_path.startswith("/"):
                default_distribution_path = "/" + default_distribution_path
            default_distribution_path += "*"

            distribution_paths = props.get('DistributionPaths', [default_distribution_path])
        except KeyError as e:
            cfn_error("missing request resource property %s. props: %s" % (str(e), props))
            return

        # configure aws cli options after resetting back to the defaults for each request
        if os.path.exists(AWS_CLI_CONFIG_FILE):
                os.remove(AWS_CLI_CONFIG_FILE)
        if sign_content:
                aws_command("configure", "set", "default.s3.payload_signing_enabled", "true")

        # treat "/" as if no prefix was specified
        if dest_bucket_prefix == "/":
            dest_bucket_prefix = ""

        s3_source_zips = list(map(lambda name, key: "s3://%s/%s" % (name, key), source_bucket_names, source_object_keys))
        s3_dest = "s3://%s/%s" % (dest_bucket_name, dest_bucket_prefix)
        old_s3_dest = "s3://%s/%s" % (old_props.get("DestinationBucketName", ""), old_props.get("DestinationBucketKeyPrefix", ""))


        # obviously this is not
        if old_s3_dest == "s3:///":
            old_s3_dest = None

        logger.info("| s3_dest: %s" % sanitize_message(s3_dest))
        logger.info("| old_s3_dest: %s" % sanitize_message(old_s3_dest))

        # if we are creating a new resource, allocate a physical id for it
        # otherwise, we expect physical id to be relayed by cloudformation
        if request_type == "Create":
            physical_id = "aws.cdk.s3deployment.%s" % str(uuid4())
        else:
            if not physical_id:
                cfn_error("invalid request: request type is '%s' but 'PhysicalResourceId' is not defined" % request_type)
                return

        # delete or create/update (only if "retain_on_delete" is false)
        if request_type == "Delete" and not retain_on_delete:
            if not bucket_owned(dest_bucket_name, dest_bucket_prefix):
                aws_command("s3", "rm", s3_dest, "--recursive")

        # if we are updating without retention and the destination changed, delete first
        if request_type == "Update" and not retain_on_delete and old_s3_dest != s3_dest:
            if not old_s3_dest:
                logger.warn("cannot delete old resource without old resource properties")
                return

            aws_command("s3", "rm", old_s3_dest, "--recursive")

        if request_type == "Update" or request_type == "Create":
            s3_deploy(s3_source_zips, s3_dest, user_metadata, system_metadata, prune, exclude, include, source_markers, extract, source_markers_config, sign_content)

        # after the sync has run, optionally look up the S3 version IDs of the deployed objects.
        # only performed when the consumer opted in by reading `objectVersionIds` (extract=false).
        object_version_ids = []
        if output_object_version_ids and (request_type == "Update" or request_type == "Create"):
            object_version_ids = get_object_version_ids(dest_bucket_name, dest_bucket_prefix, source_object_keys)

        if distribution_id:
            cloudfront_invalidate(distribution_id, distribution_paths, wait_for_distribution_invalidation)

        cfn_send(event, context, CFN_SUCCESS, physicalResourceId=physical_id, responseData={
            # Passing through the ARN sequences dependencees on the deployment
            'DestinationBucketArn': props.get('DestinationBucketArn'),
            **({'SourceObjectKeys': props.get('SourceObjectKeys')} if output_object_keys else {'SourceObjectKeys': []}),
            # When requested, always include the key (even on delete/no-op, where the list is empty)
            # so Fn::GetAtt on SourceObjectVersionIds never fails. Omitted entirely when not requested.
            **({'SourceObjectVersionIds': object_version_ids} if output_object_version_ids else {})
        })
    except KeyError as e:
        cfn_error("invalid request. Missing key %s" % str(e))
    except Exception as e:
        logger.exception("| unhandled error: %s" % sanitize_message(str(e)))
        cfn_error(str(e))

#---------------------------------------------------------------------------------------------------
# Sanitize the message to mitigate CWE-117 and CWE-93 vulnerabilities
def sanitize_message(message):
    if not message:
        return message

    # Convert non-string types to string for sanitization
    if not isinstance(message, str):
        message = str(message)

    # Sanitize the message to prevent log injection and HTTP response splitting
    sanitized_message = message.replace('\n', '').replace('\r', '')

    # Encode the message to handle special characters
    encoded_message = urllib.parse.quote(sanitized_message)

    return encoded_message

#---------------------------------------------------------------------------------------------------
# populate all files from s3_source_zips to a destination bucket
def s3_deploy(s3_source_zips, s3_dest, user_metadata, system_metadata, prune, exclude, include, source_markers, extract, source_markers_config, sign_content=False):
    if len(s3_source_zips) < len(source_markers):
        raise Exception(f"Can never have more SourceMarkers ({len(source_markers)}) than SourceObjectKeys ({len(s3_source_zips)})")

    # The common static-asset case does not need a materialized extraction tree.
    # Reconcile archive members directly against S3 object hashes instead of
    # spawning a CLI transfer and re-uploading unchanged extracted files.
    # Retain CLI sync for substitution/metadata and unextracted-archive paths.
    if extract and not sign_content and not any(source_markers) and not user_metadata and not system_metadata:
        return s3_deploy_zip_members(s3_source_zips, s3_dest, prune, exclude, include)

    # create a temporary working directory in /tmp or if enabled an attached efs volume
    if ENV_KEY_MOUNT_PATH in os.environ:
        workdir = os.getenv(ENV_KEY_MOUNT_PATH) + "/" + str(uuid4())
        os.mkdir(workdir)
    else:
        workdir = tempfile.mkdtemp()

    logger.info("| workdir: %s" % workdir)

    # create a directory into which we extract the contents of the zip file
    contents_dir=os.path.join(workdir, 'contents')
    os.mkdir(contents_dir)

    try:
        # Count archive members while extracting, without an extra pass over their contents.
        # The S3 CLI transfer pool is the critical path for large static asset trees.
        file_count = 0
        # download the archive from the source and extract to "contents"
        for i in range(len(s3_source_zips)):
            s3_source_zip = s3_source_zips[i]
            markers       = source_markers[i] if i < len(source_markers) else {}
            markers_config = source_markers_config[i] if i < len(source_markers_config) else {}

            if extract:
                archive=os.path.join(workdir, str(uuid4()))
                logger.info("archive: %s" % archive)
                aws_command("s3", "cp", s3_source_zip, archive)
                logger.info("| extracting archive to: %s\n" % contents_dir)
                logger.info("| markers: %s" % sanitize_message(markers))
                file_count += extract_and_replace_markers(archive, contents_dir, markers, markers_config)
            else:
                logger.info("| copying archive to: %s\n" % contents_dir)
                aws_command("s3", "cp", s3_source_zip, contents_dir)
                file_count += 1

        # Keep aws s3 sync as the source of truth for filters, metadata, object
        # comparisons and pruning. Only its transfer pool needs to scale with
        # the number of objects; the default ten threads bottleneck big trees.
        configure_sync_parallelism(file_count)

        # sync from "contents" to destination

        s3_command = ["s3", "sync"]

        if prune:
          s3_command.append("--delete")

        if exclude:
          for filter in exclude:
            s3_command.extend(["--exclude", filter])

        if include:
          for filter in include:
            s3_command.extend(["--include", filter])

        s3_command.extend([contents_dir, s3_dest])
        s3_command.extend(create_metadata_args(user_metadata, system_metadata))
        aws_command(*s3_command)
    finally:
        if not os.getenv(ENV_KEY_SKIP_CLEANUP):
            shutil.rmtree(workdir)


#---------------------------------------------------------------------------------------------------
def s3_location(uri):
    """Split an S3 URI while preserving opaque object-key characters."""
    if not uri.startswith("s3://"):
        raise ValueError("Expected an s3://bucket/key URL")
    bucket, separator, key = uri[5:].partition("/")
    if not bucket or not separator:
        raise ValueError("Expected an s3://bucket/key URL")
    # S3 keys are opaque: '#' and '?' are literal characters here, not
    # URL fragments or query strings. Do not percent-decode a literal key.
    return bucket, key


def zip_member_name(filename):
    """Match ZipFile.extractall's relative-path normalization on Lambda (POSIX)."""
    return "/".join(piece for piece in filename.split("/") if piece not in ("", ".", ".."))


def included_for_sync(path, excludes, includes):
    """Match the s3 sync filter order: every exclude, then every include."""
    accepted = True
    for pattern in excludes:
        if fnmatch.fnmatchcase(path, pattern):
            accepted = False
    for pattern in includes:
        if fnmatch.fnmatchcase(path, pattern):
            accepted = True
    return accepted


def zip_member_md5(archive, member):
    digest = hashlib.md5(usedforsecurity=False)
    with archive.open(member) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_worker_count(files, largest_file_bytes, memory_mb):
    """Cap aggregate upload buffers to half the Lambda memory allocation."""
    transfer_buffer = min(largest_file_bytes, 64 * 1024 * 1024)
    memory_budget = max(1, memory_mb * 1024 * 1024 // 2)
    by_memory = max(1, memory_budget // max(1, transfer_buffer))
    return min(32, max(1, files), by_memory)


def s3_deploy_zip_members(s3_sources, s3_dest, prune, excludes, includes):
    """Stream ordered ZIP members to S3, skipping identical remote objects.

    Avoids materializing an extracted tree and unnecessary uploads. The legacy
    CLI path remains authoritative for substitution and explicit metadata.
    """
    dest_bucket, dest_prefix = s3_location(s3_dest)
    if dest_prefix and not dest_prefix.endswith("/"):
        dest_prefix += "/"

    if ENV_KEY_MOUNT_PATH in os.environ:
        workdir = os.path.join(os.getenv(ENV_KEY_MOUNT_PATH), str(uuid4()))
        os.mkdir(workdir)
    else:
        workdir = tempfile.mkdtemp()

    try:
        with contextlib.ExitStack() as stack:
            # Source archives are compressed on disk, not fully extracted. Keep
            # each archive alive until transfers finish; later sources win.
            members = {}
            for index, uri in enumerate(s3_sources):
                source_bucket, source_key = s3_location(uri)
                archive_path = os.path.join(workdir, str(index) + ".zip")
                s3.download_file(source_bucket, source_key, archive_path)
                archive = stack.enter_context(ZipFile(archive_path))
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    name = zip_member_name(member.filename)
                    if name and included_for_sync(name, excludes, includes):
                        members[name] = (archive, member)

            # Only retain metadata for desired members. Stream stale keys to
            # disk so large destination buckets do not exhaust Lambda memory.
            desired = {dest_prefix + name for name in members}
            existing = {}
            stale_path = os.path.join(workdir, "stale-objects.jsonl")
            with open(stale_path, "w", encoding="utf-8") as stale_file:
                for page in s3.get_paginator("list_objects_v2").paginate(
                        Bucket=dest_bucket, Prefix=dest_prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        if key in desired:
                            existing[key] = obj
                        elif prune and included_for_sync(
                                key[len(dest_prefix):], excludes, includes):
                            stale_file.write(json.dumps(key) + "\n")

            # The AWS CLI is not content-addressed: fresh ZIP extraction mtimes
            # cause repeated sync uploads. ETag=MD5 holds for single-part,
            # non-KMS S3 objects; otherwise upload conservatively.
            changes = []
            for name, (archive, member) in members.items():
                key = dest_prefix + name
                remote = existing.get(key)
                if remote and remote.get("Size") == member.file_size:
                    etag = remote.get("ETag", "").strip('"').lower()
                    if len(etag) == 32 and all(ch in "0123456789abcdef" for ch in etag):
                        if zip_member_md5(archive, member) == etag:
                            continue
                changes.append((key, name, archive, member))

            memory_mb = int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "1024"))
            largest_file = max((member.file_size for _, _, _, member in changes), default=0)
            workers = upload_worker_count(len(changes), largest_file, memory_mb)
            config = TransferConfig(
                multipart_threshold=64 * 1024 * 1024,
                max_concurrency=1,
                use_threads=False,
            )

            def upload(entry):
                key, name, archive, member = entry
                content_type, content_encoding = mimetypes.guess_type(name)
                args = {}
                if content_type:
                    args["ContentType"] = content_type
                if content_encoding:
                    args["ContentEncoding"] = content_encoding
                with archive.open(member) as stream:
                    s3.upload_fileobj(stream, dest_bucket, key,
                                      ExtraArgs=args, Config=config)

            # All futures complete (including on failure) before archives are
            # closed. Failed uploads cannot trigger stale-object deletion.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in pool.map(upload, changes):
                    pass

            deleted_count = 0
            if prune:
                def delete_batch(keys):
                    response = s3.delete_objects(
                        Bucket=dest_bucket,
                        Delete={"Objects": [{"Key": key} for key in keys]}
                    )
                    if response.get("Errors"):
                        raise RuntimeError("Unable to delete stale S3 objects: %s" %
                                           sanitize_message(response["Errors"]))

                batch = []
                with open(stale_path, encoding="utf-8") as stale_file:
                    for line in stale_file:
                        batch.append(json.loads(line))
                        if len(batch) == 1000:
                            delete_batch(batch)
                            deleted_count += len(batch)
                            batch.clear()
                    if batch:
                        delete_batch(batch)
                        deleted_count += len(batch)

            logger.info("| ZIP member sync: uploaded %d, unchanged %d, deleted %d" %
                        (len(changes), len(members) - len(changes), deleted_count))
    finally:
        if not os.getenv(ENV_KEY_SKIP_CLEANUP):
            shutil.rmtree(workdir)

#---------------------------------------------------------------------------------------------------
def configure_sync_parallelism(file_count):
    """Use a wider S3 transfer pool for many small objects, bounded by Lambda memory."""
    if file_count < 64:
        return

    memory_mb = int(os.environ.get('AWS_LAMBDA_FUNCTION_MEMORY_SIZE', '1024'))
    if memory_mb < 512:
        return

    workers = 32 if file_count >= 256 and memory_mb >= 1024 else 16
    logger.info("| syncing %s archive members with %s S3 transfer workers" % (file_count, workers))
    aws_command("configure", "set", "default.s3.max_concurrent_requests", str(workers))

#---------------------------------------------------------------------------------------------------
# look up the S3 version IDs of the deployed objects.
#
# for extract=false each source zip is copied through unchanged, so the destination key is
# "<dest_bucket_prefix><basename(source_object_key)>" (aws s3 cp preserves the basename and
# aws s3 sync mirrors the working directory). the returned list positionally matches
# source_object_keys. head_object on an unversioned bucket returns no VersionId, in which case
# an empty string is returned for that entry.
def get_object_version_ids(dest_bucket_name, dest_bucket_prefix, source_object_keys):
    # Normalize the prefix the same way `aws s3 sync` does: syncing a directory to
    # "s3://bucket/<prefix>" writes objects at "<prefix>/<name>" even when <prefix> has no
    # trailing slash, so join with a single "/" to match the key that was actually written.
    prefix = dest_bucket_prefix
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    version_ids = []
    for key in source_object_keys:
        dest_key = prefix + os.path.basename(key)
        try:
            resp = s3.head_object(Bucket=dest_bucket_name, Key=dest_key)
            version_ids.append(resp.get('VersionId', ''))
        except ClientError as e:
            # The object may legitimately be absent at this point (for example when an
            # include/exclude filter skipped it during the sync). Return an empty version id for it
            # rather than failing the whole deployment, and log the key so an unexpected miss is
            # still diagnosable in CloudWatch.
            if e.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                logger.info("| no object found at s3://%s/%s; returning empty version id" % (sanitize_message(dest_bucket_name), sanitize_message(dest_key)))
                version_ids.append('')
            else:
                raise
    return version_ids

#---------------------------------------------------------------------------------------------------
# invalidate files in the CloudFront distribution edge caches
def cloudfront_invalidate(distribution_id, distribution_paths, wait_for_invalidation):
    invalidation_resp = cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            'Paths': {
                'Quantity': len(distribution_paths),
                'Items': distribution_paths
            },
            'CallerReference': str(uuid4()),
        })
    if wait_for_invalidation:
        try:
            # Wait for a maximum of 13 minutes for invalidation to complete.
            cloudfront.get_waiter('invalidation_completed').wait(
                DistributionId=distribution_id,
                Id=invalidation_resp['Invalidation']['Id'],
                WaiterConfig={
                    'Delay': 20,
                    'MaxAttempts': (13*60)//20,
                }
            )
        except WaiterError as e:
            raise RuntimeError(f"Unable to confirm that cache invalidation was successful. This may be a CloudFront regression as reported in https://github.com/aws/aws-cdk/issues/15891") from e


#---------------------------------------------------------------------------------------------------
# set metadata
def create_metadata_args(raw_user_metadata, raw_system_metadata):
    if len(raw_user_metadata) == 0 and len(raw_system_metadata) == 0:
        return []

    format_system_metadata_key = lambda k: k.lower()
    format_user_metadata_key = lambda k: k.lower()

    system_metadata = { format_system_metadata_key(k): v for k, v in raw_system_metadata.items() }
    user_metadata = { format_user_metadata_key(k): v for k, v in raw_user_metadata.items() }

    flatten = lambda l: [item for sublist in l for item in sublist]
    system_args = flatten([[f"--{k}", v] for k, v in system_metadata.items()])
    user_args = ["--metadata", json.dumps(user_metadata, separators=(',', ':'))] if len(user_metadata) > 0 else []

    return system_args + user_args + ["--metadata-directive", "REPLACE"]

#---------------------------------------------------------------------------------------------------
# executes an "aws" cli command
def aws_command(*args):
    aws="/opt/awscli/aws" # from AwsCliLayer
    logger.info("| aws %s" % sanitize_message(' '.join(args)))
    subprocess.check_call([aws] + list(args))

#---------------------------------------------------------------------------------------------------
# sends a response to cloudformation
def cfn_send(event, context, responseStatus, responseData={}, physicalResourceId=None, noEcho=False, reason=None):

    responseUrl = event['ResponseURL']

    responseBody = {}
    responseBody['Status'] = responseStatus
    responseBody['Reason'] = reason or ('See the details in CloudWatch Log Stream: ' + context.log_stream_name)
    responseBody['PhysicalResourceId'] = physicalResourceId or context.log_stream_name
    responseBody['StackId'] = event['StackId']
    responseBody['RequestId'] = event['RequestId']
    responseBody['LogicalResourceId'] = event['LogicalResourceId']
    responseBody['NoEcho'] = noEcho
    responseBody['Data'] = responseData

    body = json.dumps(responseBody)
    logger.info("| response body: " + sanitize_message(body))

    headers = {
        'content-type' : '',
        'content-length' : str(len(body))
    }

    try:
        request = Request(responseUrl, method='PUT', data=bytes(body.encode('utf-8')), headers=headers)
        with contextlib.closing(urlopen(request)) as response:
            logger.info("| status code: " + response.reason)
    except Exception as e:
        logger.error("| unable to send response to CloudFormation")
        logger.exception("| unable to send response: %s" % sanitize_message(str(e)))


#---------------------------------------------------------------------------------------------------
# check if bucket is owned by a custom resource
# if it is then we don't want to delete content
def bucket_owned(bucketName, keyPrefix):
    tag = CUSTOM_RESOURCE_OWNER_TAG
    if keyPrefix != "":
        tag = tag + ':' + keyPrefix
    try:
        request = s3.get_bucket_tagging(
            Bucket=bucketName,
        )
        return any((x["Key"].startswith(tag)) for x in request["TagSet"])
    except Exception as e:
        logger.info("| error getting tags from bucket")
        logger.exception("| error getting tags: %s" % sanitize_message(str(e)))
        return False

# extract archive and replace markers in output files
def extract_and_replace_markers(archive, contents_dir, markers, markers_config):
    with ZipFile(archive, "r") as zip:
        zip.extractall(contents_dir)
        members = zip.infolist()

        # Empty marker maps are the common case for static assets: no file
        # needs to be opened or even stat'ed for substitution.
        if not markers:
            return sum(not member.is_dir() for member in members)

        # replace markers for this source
        for member in members:
            if member.is_dir():
                continue
            file_path = os.path.join(contents_dir, member.filename)
            replace_markers(file_path, markers, markers_config)
        return sum(not member.is_dir() for member in members)


def replace_markers(filename, markers, markers_config):
    """Replace markers in a file, with special handling for JSON files."""
    # if there are no markers, skip
    if not markers:
        return

    outfile = filename + '.new'

    replace_tokens = { k.encode('utf-8'): encode_marker_value(k, v, markers_config).encode('utf-8') for k, v in markers.items() }

    # Handle content with line-by-line binary replacement
    with open(filename, 'rb') as fi, open(outfile, 'wb') as fo:
        # Process line by line to handle large files
        for line in fi:
            for token, replacement in replace_tokens.items():
                line = line.replace(token, replacement)
            fo.write(line)

    # Delete the original file and rename the new one to the original
    os.remove(filename)
    os.rename(outfile, filename)


def encode_marker_value(marker_name, value, markers_config):
    """Encode a marker value based on the markers configuration."""
    json_escape = markers_config.get('jsonEscape', 'false').lower() == 'true'
    default_encoding = 'UNQUOTED_JSON_VALUE' if json_escape else 'LITERAL'

    encoding = markers_config.get('markerEncodings', {}).get(marker_name, default_encoding)

    if encoding == 'LITERAL':
        if not isinstance(value, str):
            raise ValueError(f"Expected a string for LITERAL encoding for marker {marker_name}, got {type(value).__name__}")
        return value
    elif encoding == 'UNQUOTED_JSON_VALUE':
        serialized = json.dumps(value)
        if serialized.startswith('"') and serialized.endswith('"'):
            return json.dumps(value)[1:-1]
        return serialized
    elif encoding == 'JSON_VALUE':
        return json.dumps(value)
    elif encoding == 'YAML_STRING':
        if not isinstance(value, str):
            raise ValueError(f"Expected a string for YAML_STRING encoding for marker {marker_name}, got {type(value).__name__}")
        if SAFE_PLAIN_YAML.fullmatch(value) and value.lower() not in YAML_RESERVED:
            return value
        return json.dumps(value)
    else:
        raise ValueError(f"Unknown marker encoding: {encoding}")

SAFE_PLAIN_YAML = re.compile(r'^[A-Za-z][A-Za-z0-9._-]*$')
YAML_RESERVED = {'y', 'n', 'yes', 'no', 'true', 'false', 'on', 'off', 'null'}

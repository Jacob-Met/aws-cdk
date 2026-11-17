# unit tests for the s3 bucket deployment lambda handler
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import index
from botocore.exceptions import ClientError, WaiterError
from botocore.vendored import requests

# set TEST_AWSCLI_PATH to point to the "aws" stub we have here
scriptdir=os.path.dirname(os.path.realpath(__file__))
os.environ['TEST_AWSCLI_PATH'] = os.path.join(scriptdir, 'aws')

class TestHandler(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger()

        # clean up old aws.out file (from previous runs)
        try: os.remove("aws.out")
        except OSError: pass

    def test_invalid_request(self):
        resp = invoke_handler("Create", {}, expected_status="FAILED")
        self.assertEqual(resp["Reason"], "missing request resource property 'SourceBucketNames'. props: {}")

    def test_error_logger(self):
        with patch.object(self.logger, 'error') as error_logger_mock:
            invoke_handler("Create", {}, expected_status="FAILED")
            error_logger_mock.assert_called_once_with("| cfn_error: missing%20request%20resource%20property%20%27SourceBucketNames%27.%20props%3A%20%7B%7D")

    def test_error_logger_encoding_input(self):
        with patch.object(self.logger, 'error') as error_logger_mock:
            invoke_handler("Create", {
                "Test": "random%0D%0A%5BINFO%5D%20hacking"
            }, expected_status="FAILED")
            error_logger_mock.assert_called_once_with("| cfn_error: missing%20request%20resource%20property%20%27SourceBucketNames%27.%20props%3A%20%7B%27Test%27%3A%20%27random%250D%250A%255BINFO%255D%2520hacking%27%7D")

    def test_error_logger_crlf_injection(self):
        with patch.object(self.logger, 'error') as error_logger_mock:
            invoke_handler("Create", {
                "Test": "value\r\n[INFO] injected log line"
            }, expected_status="FAILED")
            call_args = error_logger_mock.call_args[0][0]
            self.assertNotIn('\r', call_args)
            self.assertNotIn('\n', call_args)

    
    def test_cloudfront_waiter_error_message(self):
        
        def mock_make_api_call(_self, operation_name, _kwarg):
            if operation_name == 'CreateInvalidation':
                return {'Invalidation': {'Id': '<invalidation-id>'}}
            if operation_name == 'GetInvalidation':
                raise WaiterError(
                    name='invalidation_completed',
                    reason='Max attempts exceeded',
                    last_response={'Invalidation': {'Status': 'InProgress'}}
                )
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DistributionId": "<cf-dist-id>",
            }, expected_status="FAILED")

        self.assertIn("Unable to confirm that cache invalidation was successful", resp["Reason"])
        self.assertIn("CloudFront regression", resp["Reason"])

    def test_cloudfront_no_wait_invalidation(self):
        def mock_make_api_call(_self, operation_name, _kwarg):
            if operation_name == 'CreateInvalidation':
                return {'Invalidation': {'Id': '<invalidation-id>'}}
            if operation_name == 'GetInvalidation':
                raise RuntimeError('Waiting should be skipped in this test case.')
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DistributionId": "<cf-dist-id>",
                "WaitForDistributionInvalidation": False
            })

    def test_sanitize_message(self):
        sanitized = index.sanitize_message("twenty-one\r\n%0a%0aINFO:+User+logged+out%3dbadguy")

        # Expect the output sanitized string to remove newline characters and enforce double URL encoding
        self.assertEqual(sanitized, 'twenty-one%250a%250aINFO%3A%2BUser%2Blogged%2Bout%253dbadguy')

    def test_sanitize_message_non_string(self):
        sanitized = index.sanitize_message({"key": "value\r\ninjected"})
        # Non-string input should be converted to string and sanitized
        self.assertNotIn('\r', sanitized)
        self.assertNotIn('\n', sanitized)

    def test_sanitize_message_none(self):
        self.assertIsNone(index.sanitize_message(None))

    def test_aws_command_log_crlf_injection(self):
        # CRLF injected via aws_command arguments must be neutralized in the
        # "| aws ..." log line, verifying sanitization on a real logging path
        # (not just sanitize_message() in isolation).
        with patch.object(self.logger, 'info') as info_logger_mock, \
                patch('subprocess.check_call'):
            index.aws_command("s3", "cp", "s3://bucket/key\r\n[INFO] injected log line", "/tmp/dest")
            info_logger_mock.assert_called_once()
            logged = info_logger_mock.call_args[0][0]
            self.assertTrue(logged.startswith("| aws "))
            self.assertNotIn('\r', logged)
            self.assertNotIn('\n', logged)

    def test_sync_parallelism_bounded_by_archive_size_and_lambda_memory(self):
        for files, memory, workers in (
            (63, 1024, None),
            (64, 1024, 16),
            (255, 1024, 16),
            (256, 1024, 32),
            (500, 512, 16),
            (500, 256, None),
        ):
            with self.subTest(files=files, memory=memory):
                with patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_MEMORY_SIZE': str(memory)}):
                    with patch.object(index, 'aws_command') as run:
                        index.configure_sync_parallelism(files)
                if workers is None:
                    run.assert_not_called()
                else:
                    run.assert_called_once_with('configure', 'set',
                        'default.s3.max_concurrent_requests', str(workers))

    def test_empty_markers_never_read_archive_members_for_substitution(self):
        from zipfile import ZipFile
        with tempfile.TemporaryDirectory() as workdir:
            archive = os.path.join(workdir, 'source.zip')
            contents = os.path.join(workdir, 'contents')
            with ZipFile(archive, 'w') as z:
                z.writestr('assets/', b'')
                z.writestr('assets/app.wasm', b'\x00\xff' * (1024 * 1024))
                z.writestr('assets/app.js', b'const a = 1;')

            with patch.object(index, 'replace_markers', side_effect=AssertionError('unexpected scan')):
                files = index.extract_and_replace_markers(archive, contents, {}, {})
            self.assertEqual(files, 2)
            with open(os.path.join(contents, 'assets', 'app.wasm'), 'rb') as data:
                self.assertEqual(len(data.read()), 2 * 1024 * 1024)

    def test_large_zip_drives_sync_pool_without_changing_sync_semantics(self):
        import shutil
        from zipfile import ZipFile
        with tempfile.TemporaryDirectory() as workdir:
            archive = os.path.join(workdir, 'source.zip')
            with ZipFile(archive, 'w') as z:
                for i in range(300):
                    z.writestr('static/item-%03d.js' % i, b'console.log(1)')
            calls = []

            def fake_aws(*args):
                calls.append(args)
                if args[:2] == ('s3', 'cp'):
                    shutil.copyfile(archive, args[-1])
                if args[:2] == ('s3', 'sync'):
                    self.assertIn('--delete', args)
                    self.assertIn('--exclude', args)
                    self.assertIn('--include', args)
                    self.assertTrue(os.path.exists(os.path.join(args[args.index('--include') + 2], 'static', 'item-299.js')))

            with patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_MEMORY_SIZE': '1024'}):
                with patch.object(index, 'aws_command', side_effect=fake_aws):
                    index.s3_deploy(['s3://source/archive.zip'], 's3://destination/prefix',
                        {'team': 'cdn'}, {'cache-control': 'public, max-age=3600'},
                        True, ['*.tmp'], ['*.js'], [{}], True, [{}])

            self.assertEqual([call[:2] for call in calls],
                [('s3', 'cp'), ('configure', 'set'), ('s3', 'sync')])
            self.assertEqual(calls[1][-1], '32')
            self.assertIn('--metadata', calls[2])
            self.assertIn('--cache-control', calls[2])

    def test_create_update(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>"
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_no_delete(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Prune": "false"
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_no_delete(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Prune": "false"
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_exclude(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Exclude": ["sample.json"]
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--exclude", "sample.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_exclude(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Exclude": ["sample.json"]
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--exclude", "sample.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_include(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Include": ["/sample/*.json"]
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--include", "/sample/*.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_include(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Include": ["/sample/*.json"]
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--include", "/sample/*.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_include_exclude(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Exclude": ["/sample/*"],
            "Include": ["/sample/*.json"]
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--exclude", "/sample/*", "--include", "/sample/*.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_include_exclude(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Exclude": ["/sample/*"],
            "Include": ["/sample/*.json"]
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--exclude", "/sample/*", "--include", "/sample/*.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_no_extract_file(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Extract": "false"
        })

        self.assertAwsCommands(
                ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "/tmp/contents"],
                ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_no_extract_file(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Extract": "false"
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
                ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "/tmp/contents"],
                ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_multiple_include_exclude(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Exclude": ["/sample/*", "/another/*"],
            "Include": ["/sample/*.json", "/another/*.json"]
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--exclude", "/sample/*", "--exclude", "/another/*", "--include", "/sample/*.json", "--include", "/another/*.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_multiple_include_exclude(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Exclude": ["/sample/*", "/another/*"],
            "Include": ["/sample/*.json", "/another/*.json"]
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "--exclude", "/sample/*", "--exclude", "/another/*", "--include", "/sample/*.json", "--include", "/another/*.json", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_update_multiple_sources(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket1>", "<source-bucket2>"],
            "SourceObjectKeys": ["<source-object-key1>", "<source-object-key2>"],
            "DestinationBucketName": "<dest-bucket-name>"
        })

        # Note: these are different files in real-life. For testing purposes, we hijack
        #       the command to output a static filename, archive.zip
        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket1>/<source-object-key1>", "archive.zip"],
            ["s3", "cp", "s3://<source-bucket2>/<source-object-key2>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_create_with_backslash_prefix_same_as_no_prefix(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "DestinationBucketKeyPrefix": "/"
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )


    def test_create_update_with_dest_key(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "DestinationBucketKeyPrefix": "<dest-key-prefix>"
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/<dest-key-prefix>"]
        )

    def test_create_update_with_metadata(self):
        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "DestinationBucketKeyPrefix": "<dest-key-prefix>",
            "UserMetadata": { "best": "game" },
            "SystemMetadata": { "content-type": "text/html", "content-language": "en" }
        })

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/<dest-key-prefix>", "--content-type", "text/html", "--content-language", "en", "--metadata", "{\"best\":\"game\"}", "--metadata-directive", "REPLACE"]
        )

    def test_delete_no_retain(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                assert kwarg['Bucket'] == '<dest-bucket-name>'
                return {'TagSet': [{'Key': 'random', 'Value': '<logical-resource-id>'}]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)
        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "RetainOnDelete": "false"
            }, physical_id="<physicalid>")

        self.assertAwsCommands(["s3", "rm", "s3://<dest-bucket-name>/", "--recursive"])

    # In a replace the logcal id of the custom resource will change
    # so the custom resource that gets the Delete event will no longer
    # "own" the bucket
    def test_replace_no_retain(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                assert kwarg['Bucket'] == '<dest-bucket-name>'
                return {'TagSet': [{'Key': 'aws-cdk:cr-owned:-bucket>', 'Value': '<some-other-logical-id>'}]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)
        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "RetainOnDelete": "false"
            }, physical_id="<physicalid>")

        self.assertAwsCommands()

    def test_delete_with_dest_key(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                assert kwarg['Bucket'] == '<dest-bucket-name>'
                return {'TagSet': [{'Key': 'random-key', 'Value': '<logical-resource-id>'}]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)
        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DestinationBucketKeyPrefix": "<dest-key-prefix>",
                "RetainOnDelete": "false"
            }, physical_id="<physicalid>")

        self.assertAwsCommands(["s3", "rm", "s3://<dest-bucket-name>/<dest-key-prefix>", "--recursive"])

    def test_delete_with_retain_explicit(self):
        invoke_handler("Delete", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "RetainOnDelete": "true"
        }, physical_id="<physicalid>")

        # no aws commands (retain)
        self.assertAwsCommands()

    # RetainOnDelete=true is the default
    def test_delete_with_retain_implicit_default(self):
        invoke_handler("Delete", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>"
        }, physical_id="<physicalid>")

        # no aws commands (retain)
        self.assertAwsCommands()

    def test_delete_with_retain_explicitly_false(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                assert kwarg['Bucket'] == '<dest-bucket-name>'
                return {'TagSet': [{'Key': 'random-key', 'Value': '<logical-resource-id>'}]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)
        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "RetainOnDelete": "false"
            }, physical_id="<physicalid>")

        self.assertAwsCommands(
            ["s3", "rm", "s3://<dest-bucket-name>/", "--recursive"]
        )

    #
    # update
    #

    def test_update_same_dest(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_same_dest_cf_invalidate(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'CreateInvalidation':
                assert kwarg['DistributionId'] == '<cf-dist-id>'
                assert kwarg['InvalidationBatch']['Paths']['Quantity'] == 1
                assert kwarg['InvalidationBatch']['Paths']['Items'][0] == '/*'
                return {'Invalidation': {'Id': '<invalidation-id>'}}
            if operation_name == 'GetInvalidation' and kwarg['Id'] == '<invalidation-id>':
                return {'Invalidation': {'Id': '<invalidation-id>', 'Status': 'Completed'}}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Update", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DistributionId": "<cf-dist-id>"
            }, old_resource_props={
                "DestinationBucketName": "<dest-bucket-name>",
            }, physical_id="<physical-id>")

    def test_update_same_dest_cf_invalidate_custom_prefix(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'CreateInvalidation':
                assert kwarg['DistributionId'] == '<cf-dist-id>'
                assert kwarg['InvalidationBatch']['Paths']['Quantity'] == 1
                assert kwarg['InvalidationBatch']['Paths']['Items'][0] == '/<dest-prefix>/*'
                return {'Invalidation': {'Id': '<invalidation-id>'}}
            if operation_name == 'GetInvalidation' and kwarg['Id'] == '<invalidation-id>':
                return {'Invalidation': {'Id': '<invalidation-id>', 'Status': 'Completed'}}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Update", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DestinationBucketKeyPrefix": "<dest-prefix>",
                "DistributionId": "<cf-dist-id>"
            }, old_resource_props={
                "DestinationBucketName": "<dest-bucket-name>",
            }, physical_id="<physical-id>")

    def test_update_same_dest_cf_invalidate_custom_paths(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'CreateInvalidation':
                assert kwarg['DistributionId'] == '<cf-dist-id>'
                assert kwarg['InvalidationBatch']['Paths']['Quantity'] == 2
                assert kwarg['InvalidationBatch']['Paths']['Items'][0] == '/path1/*'
                assert kwarg['InvalidationBatch']['Paths']['Items'][1] == '/path2/*'
                return {'Invalidation': {'Id': '<invalidation-id>'}}
            if operation_name == 'GetInvalidation' and kwarg['Id'] == '<invalidation-id>':
                return {'Invalidation': {'Id': '<invalidation-id>', 'Status': 'Completed'}}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Update", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DistributionId": "<cf-dist-id>",
                "DistributionPaths": ["/path1/*", "/path2/*"]
            }, old_resource_props={
                "DestinationBucketName": "<dest-bucket-name>",
            }, physical_id="<physical-id>")

    def test_update_new_dest_retain(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
            "RetainOnDelete": "true"
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/"]
        )

    def test_update_new_dest_no_retain(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<new-dest-bucket-name>",
            "RetainOnDelete": "false"
        }, old_resource_props={
            "DestinationBucketName": "<old-dest-bucket-name>",
            "DestinationBucketKeyPrefix": "<old-dest-prefix>",
            "RetainOnDelete": "false"
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "rm", "s3://<old-dest-bucket-name>/<old-dest-prefix>", "--recursive"],
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<new-dest-bucket-name>/"]
        )

    def test_update_new_dest_retain_implicit(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<new-dest-bucket-name>",
        }, old_resource_props={
            "DestinationBucketName": "<old-dest-bucket-name>",
            "DestinationBucketKeyPrefix": "<old-dest-prefix>"
        }, physical_id="<physical-id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<new-dest-bucket-name>/"]
        )

    def test_update_new_dest_prefix_no_retain(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "DestinationBucketKeyPrefix": "<new-dest-prefix>",
            "RetainOnDelete": "false"
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
            "RetainOnDelete": "false"
        }, physical_id="<physical id>")

        self.assertAwsCommands(
            ["s3", "rm", "s3://<dest-bucket-name>/", "--recursive"],
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/<new-dest-prefix>"]
        )

    def test_update_new_dest_prefix_retain_implicit(self):
        invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "DestinationBucketKeyPrefix": "<new-dest-prefix>"
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id="<physical id>")

        self.assertAwsCommands(
            ["s3", "cp", "s3://<source-bucket>/<source-object-key>", "archive.zip"],
            ["s3", "sync", "--delete", "contents.zip", "s3://<dest-bucket-name>/<new-dest-prefix>"]
        )

    #
    # physical id
    #

    def test_physical_id_allocated_on_create_and_reused_afterwards(self):

        create_resp = invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
        })

        phid = create_resp['PhysicalResourceId']
        self.assertTrue(phid.startswith('aws.cdk.s3deployment'))

        # now issue an update and pass in the physical id. expect the same
        # one to be returned back
        update_resp = invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<new-dest-bucket-name>",
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, physical_id=phid)
        self.assertEqual(update_resp['PhysicalResourceId'], phid)

        # now issue a delete, and make sure this also applies
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                return {'TagSet': [{'Key': 'aws-cdk:cr-owned:-bucket>', 'Value': '<logical-resource-id>'}]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)
        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            delete_resp = invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "RetainOnDelete": "false"
            }, physical_id=phid)
        self.assertEqual(delete_resp['PhysicalResourceId'], phid)

    def test_fails_when_physical_id_not_present_in_update(self):
        update_resp = invoke_handler("Update", {
            "SourceBucketNames": ["<source-bucket>"],
            "SourceObjectKeys": ["<source-object-key>"],
            "DestinationBucketName": "<new-dest-bucket-name>",
        }, old_resource_props={
            "DestinationBucketName": "<dest-bucket-name>",
        }, expected_status="FAILED")

        self.assertEqual(update_resp['Reason'], "invalid request: request type is 'Update' but 'PhysicalResourceId' is not defined")

    def test_fails_when_physical_id_not_present_in_delete(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                return {'TagSet': [{'Key': 'aws-cdk:cr-owned:-bucket>', 'Value': '<logical-resource-id>'}]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            update_resp = invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<new-dest-bucket-name>",
            }, old_resource_props={
                "DestinationBucketName": "<dest-bucket-name>",
            }, expected_status="FAILED")

        self.assertEqual(update_resp['Reason'], "invalid request: request type is 'Delete' but 'PhysicalResourceId' is not defined")

    def test_physical_id_on_cloud_front_error(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'CreateInvalidation':
                raise ClientError({'Error': {'Code': '500', 'Message': 'Invalidation error'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Update", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DistributionId": "<cf-dist-id>",
            }, old_resource_props={
                "DestinationBucketName": "<dest-bucket-name>",
            }, physical_id="<physical-id>", expected_status="FAILED")

        self.assertEqual(resp["PhysicalResourceId"], "<physical-id>")

    # no bucket tags removes content
    def test_no_tags_on_bucket(self):
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                raise ClientError({'Error': {'Code': 'NoSuchTagSet', 'Message': 'The TagSet does not exist'}}, operation_name)
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "RetainOnDelete": "false"
            }, physical_id="<physicalid>")

        self.assertAwsCommands(
            ["s3", "rm", "s3://<dest-bucket-name>/", "--recursive"]
        )
    
    def test_replace_markers(self):
        for json_aware_source_processing in [True, False]:
            with self.subTest(json_aware_source_processing=json_aware_source_processing):
                marker_config = {}
                if json_aware_source_processing:
                    marker_config = {"jsonEscape": "true"}
                index.extract_and_replace_markers("test.zip", "/tmp/out", {
                    "_marker3_": "value\"with\"quotes",
                    "_marker2_": "boom-marker2-replaced",
                    "_marker1_": "<<foo>>",
                }, marker_config)

                # assert that markers were replaced in the output
                with open("/tmp/out/subfolder/boom.txt", "r") as file:
                    self.assertEqual(file.read().rstrip(), "Another <<foo>> file with boom-marker2-replaced hey!\nLine 2 with <<foo>> again :-)")

                with open("/tmp/out/test.txt") as file:
                    self.assertEqual(file.read().rstrip(), "Hello, <<foo>> world")

                with open("/tmp/out/test.yaml") as file:
                    import yaml
                    content = file.read().rstrip()
                    yamlObject = yaml.safe_load(content) # valid yaml
                    if json_aware_source_processing:
                        self.assertEqual(content, "root:\n    key_with_quote: prefixvalue\\\"with\\\"quotessuffix\n    key_without_quote: prefixboom-marker2-replacedsuffix")
                    else:
                        self.assertEqual(content, "root:\n    key_with_quote: prefixvalue\"with\"quotessuffix\n    key_without_quote: prefixboom-marker2-replacedsuffix")

                with open("/tmp/out/test.json") as file:
                    content = file.read().rstrip()
                    if json_aware_source_processing:
                        # self.assertEqual(content, '{"root": {"key_with_quote": "prefixvalue\\"with\\"quotessuffix", "key_without_quote": "prefixboom-marker2-replacedsuffix"}}')
                        self.assertEqual(content, '{\n    "root": {\n        "key_with_quote": "prefixvalue\\"with\\"quotessuffix",\n        "key_without_quote": "prefixboom-marker2-replacedsuffix"\n    }\n}')
                        jsonObject = json.loads(content) # valid json
                    else:
                        self.assertEqual(content, '{\n    "root": {\n        "key_with_quote": "prefixvalue"with"quotessuffix",\n        "key_without_quote": "prefixboom-marker2-replacedsuffix"\n    }\n}')

    def test_marker_encodings(self):
        cases = [
            ('LITERAL', 'a"b', 'a"b'),
            ('UNQUOTED_JSON_VALUE', 'a"b\\c', 'a\\"b\\\\c'),
            ('UNQUOTED_JSON_VALUE', ['a"1', 'b'], '["a\\"1", "b"]'),
            ('JSON_VALUE', 'a"b', '"a\\"b"'),
            ('JSON_VALUE', ['a', 'b'], '["a", "b"]'),
            ('JSON_VALUE', {'k': 'v'}, '{"k": "v"}'),
        ]
        for encoding, value, expected in cases:
            with self.subTest(encoding=encoding, value=value):
                self.assertEqual(index.encode_marker_value('m', value, {'markerEncodings': {'m': encoding}}), expected)

    def test_yaml_string_encoding_keeps_values_strings(self):
        import yaml
        # Each value must come back as the same string from a YAML parser, whatever it looks like
        values = ['plain', 'arn:aws:s3:::bucket', 'a b', '', 'test"with"quotes', 'line\nbreak',
                  'true', 'True', 'NULL', 'yes', 'Off', 'y', 'n', '~', '.inf', '.NaN', '0x1F', '0o17',
                  '42', '1.5', '012345678901', '2024-01-01', '-', '#comment', 'a: b']
        for value in values:
            with self.subTest(value=value):
                rendered = index.encode_marker_value('m', value, {'markerEncodings': {'m': 'YAML_STRING'}})
                self.assertEqual(yaml.safe_load('v: ' + rendered)['v'], value)

        # A value that is unambiguous stays unquoted
        self.assertEqual(index.encode_marker_value('m', 'plain', {'markerEncodings': {'m': 'YAML_STRING'}}), 'plain')

    def test_default_marker_encoding_follows_json_escape(self):
        # Templates synthesized before per-marker encodings only carry `jsonEscape`, and must render as before
        self.assertEqual(index.encode_marker_value('m', 'a"b', {}), 'a"b')
        self.assertEqual(index.encode_marker_value('m', 'a"b', {'jsonEscape': 'true'}), 'a\\"b')
        self.assertEqual(index.encode_marker_value('m', ['a'], {'jsonEscape': 'true'}), '["a"]')

    def test_marker_encoding_overrides_json_escape_for_that_marker_only(self):
        config = {'jsonEscape': 'true', 'markerEncodings': {'m': 'JSON_VALUE'}}
        self.assertEqual(index.encode_marker_value('m', 'a"b', config), '"a\\"b"')
        self.assertEqual(index.encode_marker_value('other', 'a"b', config), 'a\\"b')

    def test_string_encodings_reject_non_strings_without_revealing_the_value(self):
        for encoding in ['LITERAL', 'YAML_STRING']:
            with self.subTest(encoding=encoding):
                with self.assertRaises(ValueError) as cm:
                    index.encode_marker_value('m', ['secret-value'], {'markerEncodings': {'m': encoding}})
                self.assertNotIn('secret-value', str(cm.exception))

    def test_unknown_marker_encoding_is_rejected(self):
        with self.assertRaises(ValueError):
            index.encode_marker_value('m', 'x', {'markerEncodings': {'m': 'SOMETHING_NEW'}})

    def test_marker_substitution(self):
        outdir = tempfile.mkdtemp()

        invoke_handler("Create", {
            "SourceBucketNames": ["<source-bucket>", "<source2>"],
            "SourceObjectKeys": ["<source-object-key>", "<source2-object-key>"],
            "DestinationBucketName": "<dest-bucket-name>",
            "Prune": "false",
            "SourceMarkers": [
                { "_marker1_": "value1-source1", "_marker2_": "value2-source1" },
                { "_marker1_": "value1-source2" },
            ],
        }, outdir=outdir)

        # outdir is expected to have a single directory that contains the workdir
        files = os.listdir(outdir)
        self.assertEqual(len(files), 1) # defensive

        workdir = os.path.join(outdir, files[0], "contents")

        with open(os.path.join(workdir, "test.txt"), "r") as file:
            self.assertEqual(file.read().rstrip(), "Hello, value1-source2 world")

        with open(os.path.join(workdir, "subfolder", "boom.txt"), "r") as file:
            self.assertEqual(file.read().rstrip(), "Another value1-source2 file with _marker2_ hey!\nLine 2 with value1-source2 again :-)")


    def test_output_object_version_ids(self):
        # extract=false, flag on: handler head_objects the destination key and returns its VersionId
        head_calls = []
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'HeadObject':
                head_calls.append(kwarg)
                return {'VersionId': '<version-id>'}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
            })

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], ["<version-id>"])
        self.assertEqual(len(head_calls), 1)
        self.assertEqual(head_calls[0]["Bucket"], "<dest-bucket-name>")
        self.assertEqual(head_calls[0]["Key"], "<source-object-key>")

    def test_output_object_version_ids_with_prefix(self):
        # destination key is "<prefix>/<basename(source key)>" (prefix already has a trailing slash)
        head_calls = []
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'HeadObject':
                head_calls.append(kwarg)
                return {'VersionId': '<version-id>'}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["some/path/object.zip"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DestinationBucketKeyPrefix": "my-prefix/",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
            })

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], ["<version-id>"])
        self.assertEqual(head_calls[0]["Key"], "my-prefix/object.zip")

    def test_output_object_version_ids_prefix_without_trailing_slash(self):
        # `aws s3 sync <dir> s3://bucket/my-prefix` writes objects at "my-prefix/<name>", so the
        # handler must insert the "/" it would otherwise be missing (regression guard).
        head_calls = []
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'HeadObject':
                head_calls.append(kwarg)
                return {'VersionId': '<version-id>'}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["some/path/object.zip"],
                "DestinationBucketName": "<dest-bucket-name>",
                "DestinationBucketKeyPrefix": "my-prefix",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
            })

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], ["<version-id>"])
        self.assertEqual(head_calls[0]["Key"], "my-prefix/object.zip")

    def test_output_object_version_ids_multiple_sources(self):
        # positionally matches SourceObjectKeys
        versions = {"<key1>": "<v1>", "<key2>": "<v2>"}
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'HeadObject':
                return {'VersionId': versions[kwarg["Key"]]}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket1>", "<source-bucket2>"],
                "SourceObjectKeys": ["<key1>", "<key2>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
            })

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], ["<v1>", "<v2>"])

    def test_output_object_version_ids_unversioned_bucket_returns_empty(self):
        # head_object on an unversioned bucket returns no VersionId -> empty string
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'HeadObject':
                return {}
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
            })

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], [""])

    def test_output_object_version_ids_missing_object_returns_empty(self):
        # a filtered-out (absent) destination object 404s on head_object -> empty string, no failure
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'HeadObject':
                raise ClientError({'Error': {'Code': '404', 'Message': 'Not Found'}}, operation_name)
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
            })

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], [""])

    def test_no_object_version_ids_when_flag_absent(self):
        # default: no HeadObject calls are made and the attribute is not returned
        def mock_make_api_call(self, operation_name, kwarg):
            raise Exception("no S3 API call should be made when OutputObjectVersionIds is not set, got %s" % operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Create", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "Extract": "false",
            })

        self.assertNotIn("SourceObjectVersionIds", resp["Data"])

    def test_object_version_ids_on_delete_returns_empty(self):
        # on delete the objects are gone; when requested the attribute is present as []
        def mock_make_api_call(self, operation_name, kwarg):
            if operation_name == 'GetBucketTagging':
                return {'TagSet': [{'Key': 'random-key', 'Value': '<logical-resource-id>'}]}
            if operation_name == 'HeadObject':
                raise Exception("HeadObject should not be called on delete")
            raise ClientError({'Error': {'Code': '500', 'Message': 'Unsupported operation'}}, operation_name)

        with patch('botocore.client.BaseClient._make_api_call', new=mock_make_api_call):
            resp = invoke_handler("Delete", {
                "SourceBucketNames": ["<source-bucket>"],
                "SourceObjectKeys": ["<source-object-key>"],
                "DestinationBucketName": "<dest-bucket-name>",
                "Extract": "false",
                "OutputObjectVersionIds": "true",
                "RetainOnDelete": "false",
            }, physical_id="<physicalid>")

        self.assertEqual(resp["Data"]["SourceObjectVersionIds"], [])

    # asserts that a given list of "aws xxx" commands have been invoked (in order)
    def assertAwsCommands(self, *expected):
        actual = read_aws_out()
        self.assertEqual(actual, list(expected))

# ==================================================================================================
# helpers

#
# reads "aws.out" and returns a list of "aws" commands (as strings)
def read_aws_out():
    if not os.path.exists("aws.out"):
        return []

    with open("aws.out") as f:
        return [json.loads(l) for l in f.read().splitlines()]

#
# invokes the handler under test
#   requestType: CloudFormation request type ("Create", "Update", "Delete")
#   resourceProps: map to pass to "ResourceProperties"
#   expected_status: "SUCCESS" or "FAILED"
def invoke_handler(requestType, resourceProps, old_resource_props=None, physical_id=None, expected_status='SUCCESS', outdir=None):
    response_url = 'http://<response-url>'

    event={
        'ResponseURL': response_url,
        'StackId': '<stack-id>',
        'RequestId': '<request-id>',
        'LogicalResourceId': '<logical-resource-id>',
        'RequestType': requestType,
        'ResourceProperties': resourceProps
    }

    if old_resource_props:
        event['OldResourceProperties'] = old_resource_props

    if physical_id:
        event['PhysicalResourceId'] = physical_id

    class ContextMock: log_stream_name = 'log_stream'
    class ResponseMock:
      reason = 'OK'
      # needed because the context manager calls this
      close = lambda _: _

    context = ContextMock()
    index.urlopen = MagicMock(return_value=ResponseMock())

    # control the output directory and skip cleanup so we can examine the output
    if outdir:
        os.environ[index.ENV_KEY_MOUNT_PATH] = outdir
        os.environ[index.ENV_KEY_SKIP_CLEANUP] = "1"

    # These legacy CLI contract tests assert the substitution-capable path.
    # Exercise the new marker-free default separately with a real ZIP/S3 stub.
    if (requestType in ('Create', 'Update') and
            resourceProps.get('Extract', 'true') == 'true' and
            'SourceMarkers' not in resourceProps and 'SourceObjectKeys' in resourceProps):
        event['ResourceProperties'] = dict(resourceProps)
        event['ResourceProperties']['SourceMarkers'] = [
            {'__legacy_fixture_marker__': 'unused'}
            for _ in resourceProps['SourceObjectKeys']
        ]

    #--------------------
    # invoke the handler
    #--------------------
    index.handler(event, context)

    index.urlopen.assert_called_once()
    (pos_args, _) = index.urlopen.call_args

    actual_request = pos_args[0]
    actual_url = actual_request.full_url
    actual_data = actual_request.data
    actual_method = actual_request.method

    if actual_url != response_url:
        raise Exception("Invalid url used for sending CFN response. expected=%s actual=%s" % (response_url, actual_url))

    if actual_method != 'PUT':
        raise Exception("Invalid method used for sending CFN response. expected=PUT actual=%s" % (actual_method,))

    resp = json.loads(actual_data)

    def assert_field(name, expect=None):
        value=resp.get(name)

        if not expect:
            if not resp.get(name):
                raise Exception("Missing '%s' field from response: %s" % (name, resp))
        elif expect and value != expect:
            raise Exception("Expecting response field '%s' to be '%s' but got '%s'.\n%s" % (name, expect, value, json.dumps(resp, indent=2)))

    assert_field('Status', expected_status)
    assert_field('Reason')
    assert_field('PhysicalResourceId')
    assert_field('StackId', '<stack-id>')
    assert_field('RequestId', '<request-id>')
    assert_field('LogicalResourceId', '<logical-resource-id>')

    return resp


class FakeArchiveS3:
    """Deterministic ZIP-source and S3-object model; no AWS access required."""
    def __init__(self):
        self.sources = {}
        self.objects = {}
        self.uploads = []
        self.deletes = []
        self.delete_batches = []
        self.fail_key = None

    def add_zip(self, bucket, key, files):
        from io import BytesIO
        from zipfile import ZipFile, ZIP_DEFLATED
        data = BytesIO()
        with ZipFile(data, 'w', compression=ZIP_DEFLATED) as z:
            for name, body in files.items():
                z.writestr(name, body)
        self.sources[(bucket, key)] = data.getvalue()

    def add_object(self, bucket, key, body, etag=None):
        import hashlib
        self.objects[(bucket, key)] = {
            'Body': body, 'Size': len(body),
            'ETag': etag or '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"',
        }

    def download_file(self, bucket, key, filename):
        with open(filename, 'wb') as f:
            f.write(self.sources[(bucket, key)])

    def get_paginator(self, action):
        assert action == 'list_objects_v2'
        return self

    def paginate(self, Bucket, Prefix):
        return [{'Contents': [
            {'Key': key, 'Size': val['Size'], 'ETag': val['ETag']}
            for (bucket, key), val in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]}]

    def upload_fileobj(self, stream, bucket, key, ExtraArgs=None, Config=None):
        if key == self.fail_key:
            raise RuntimeError('injected transfer failure')
        body = stream.read()
        self.add_object(bucket, key, body)
        self.objects[(bucket, key)]['ExtraArgs'] = ExtraArgs
        self.uploads.append(key)

    def delete_objects(self, Bucket, Delete):
        self.delete_batches.append(len(Delete['Objects']))
        for item in Delete['Objects']:
            key = item['Key']
            self.objects.pop((Bucket, key), None)
            self.deletes.append(key)
        return {}


class TestArchiveAwareSync(unittest.TestCase):
    def setUp(self):
        self.fake = FakeArchiveS3()

    def deploy(self, sources, dest='s3://dst/site', prune=True,
               excludes=None, includes=None):
        with patch.object(index, 's3', self.fake):
            with patch.object(index, 'aws_command', side_effect=AssertionError('CLI must not run')):
                index.s3_deploy(sources, dest, {}, {}, prune,
                    excludes or [], includes or [], [{} for _ in sources],
                    True, [{} for _ in sources])

    def test_overlay_and_hash_based_incremental_transfer_with_same_size_change(self):
        payload = b'\x00\xff' * (1024 * 1024)
        self.fake.add_zip('src', 'one.zip', {
            'app.js': b'first', 'data.wasm': payload, 'empty/': b'',
        })
        self.fake.add_zip('src', 'two.zip', {
            'app.js': b'later', 'new.txt': b'static',
        })
        self.fake.add_object('dst', 'site/stale.txt', b'delete')
        sources = ['s3://src/one.zip', 's3://src/two.zip']

        self.deploy(sources)
        self.assertEqual(self.fake.objects[('dst', 'site/app.js')]['Body'], b'later')
        self.assertEqual(self.fake.objects[('dst', 'site/data.wasm')]['Body'], payload)
        self.assertEqual(self.fake.objects[('dst', 'site/new.txt')]['Body'], b'static')
        self.assertNotIn(('dst', 'site/stale.txt'), self.fake.objects)
        self.assertEqual(sorted(self.fake.uploads),
                         ['site/app.js', 'site/data.wasm', 'site/new.txt'])
        self.assertEqual(len(self.fake.deletes), 1)

        self.deploy(sources)
        self.assertEqual(len(self.fake.uploads), 3)  # unchanged objects never re-upload
        self.assertEqual(len(self.fake.deletes), 1)

        # Same-size mutations must NOT be skipped (unlike --size-only).
        self.fake.add_zip('src', 'two.zip', {'app.js': b'other', 'new.txt': b'static'})
        self.deploy(sources)
        self.assertEqual(self.fake.uploads[-1], 'site/app.js')
        self.assertEqual(len(self.fake.uploads), 4)
        self.assertEqual(self.fake.objects[('dst', 'site/app.js')]['Body'], b'other')

    def test_filters_prefix_and_pruning_preserve_excluded_objects(self):
        self.fake.add_zip('src', 'site.zip', {
            'assets/app.js': b'keep',
            'secret/private.txt': b'not uploaded',
            'secret/public.js': b'yes',
        })
        self.fake.add_object('dst', 'site/old.js', b'stale')
        self.fake.add_object('dst', 'site/secret/private.txt', b'keep excluded')
        self.fake.add_object('dst', 'elsewhere/old.js', b'outside')
        self.deploy(['s3://src/site.zip'], excludes=['secret/*'],
                    includes=['secret/public.js'])
        self.assertEqual(sorted(self.fake.uploads),
                         ['site/assets/app.js', 'site/secret/public.js'])
        self.assertEqual(self.fake.deletes, ['site/old.js'])
        self.assertIn(('dst', 'site/secret/private.txt'), self.fake.objects)
        self.assertIn(('dst', 'elsewhere/old.js'), self.fake.objects)

    def test_failed_upload_cannot_prune_or_report_success(self):
        self.fake.add_zip('src', 'site.zip', {'ok.js': b'ok', 'broken.js': b'break'})
        self.fake.add_object('dst', 'site/stale.js', b'stale')
        self.fake.fail_key = 'site/broken.js'
        with self.assertRaisesRegex(RuntimeError, 'injected transfer failure'):
            self.deploy(['s3://src/site.zip'])
        self.assertIn(('dst', 'site/stale.js'), self.fake.objects)
        self.assertEqual(self.fake.deletes, [])

    def test_opaque_remote_etag_never_skips_safely(self):
        self.fake.add_zip('src', 'site.zip', {'binary.dat': b'unchanged'})
        self.fake.add_object('dst', 'site/binary.dat', b'unchanged', '"opaque-2"')
        self.deploy(['s3://src/site.zip'])
        self.assertEqual(self.fake.uploads, ['site/binary.dat'])

    def test_unsafe_archive_paths_are_normalized_like_extractall(self):
        self.fake.add_zip('src', 'site.zip', {'../../escape.txt': b'a', 'a/../b.txt': b'b'})
        self.deploy(['s3://src/site.zip'], dest='s3://dst/')
        self.assertEqual(sorted(self.fake.uploads), ['a/b.txt', 'escape.txt'])

    def test_large_destination_prunes_in_bounded_batches(self):
        self.fake.add_zip('src', 'site.zip', {'current.js': b'keep'})
        for i in range(1005):
            self.fake.add_object('dst', 'site/stale-%04d.bin' % i, b'stale')
        self.deploy(['s3://src/site.zip'])
        self.assertEqual(self.fake.delete_batches, [1000, 5])
        self.assertEqual(len(self.fake.deletes), 1005)
        self.assertEqual(self.fake.objects[('dst', 'site/current.js')]['Body'], b'keep')

    def test_upload_concurrency_scales_with_objects_and_stays_memory_bounded(self):
        mib = 1024 * 1024
        for files, largest, memory, expected in [
            (1000, 4096, 1024, 32),
            (200, 11 * mib, 1024, 32),
            (40, 28 * mib, 1024, 18),
            (1000, 64 * mib, 1024, 8),
            (1000, 64 * mib, 512, 4),
            (1000, 64 * mib, 128, 1),
            (0, 0, 1024, 1),
        ]:
            with self.subTest(files=files, largest=largest, memory=memory):
                workers = index.upload_worker_count(files, largest, memory)
                self.assertEqual(workers, expected)
                self.assertLessEqual(workers * min(largest, 64 * mib),
                                     max(1, memory * mib // 2))

    def test_s3_paths_preserve_opaque_object_keys(self):
        self.assertEqual(index.s3_location('s3://a/prefix/a#b?c%20d'),
                         ('a', 'prefix/a#b?c%20d'))
        self.assertEqual(index.s3_location('s3://a//leading'),
                         ('a', '/leading'))
        for invalid in ('bucket/key', 's3://', 's3:///key'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    index.s3_location(invalid)

    def test_handler_default_markerless_runs_direct_path_and_invalidates_cdn(self):
        self.fake.add_zip('assets', 'source.zip', {'index.html': b'<h1>It works</h1>'})
        event = {
            'RequestType': 'Create',
            'ResponseURL': 'https://example.invalid/cfn',
            'StackId': 'stack',
            'RequestId': 'request',
            'LogicalResourceId': 'deployment',
            'ResourceProperties': {
                'SourceBucketNames': ['assets'],
                'SourceObjectKeys': ['source.zip'],
                'DestinationBucketName': 'dst',
                'DestinationBucketKeyPrefix': 'app',
                'DistributionId': 'DIST1',
            },
        }
        with patch.object(index, 's3', self.fake):
            with patch.object(index, 'aws_command', side_effect=AssertionError('CLI forbidden')):
                with patch.object(index, 'cloudfront_invalidate') as invalidate:
                    with patch.object(index, 'cfn_send') as send:
                        index.handler(event, object())
        self.assertEqual(self.fake.objects[('dst', 'app/index.html')]['Body'],
                         b'<h1>It works</h1>')
        invalidate.assert_called_once_with('DIST1', ['/app/*'], True)
        self.assertEqual(send.call_args.args[2], index.CFN_SUCCESS)

    def test_sign_content_preserves_explicit_cli_payload_signing(self):
        self.fake.add_zip('src', 'site.zip', {'app.js': b'hello'})
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            archive = os.path.join(temp, 'source.zip')
            with open(archive, 'wb') as f:
                f.write(self.fake.sources[('src', 'site.zip')])
            calls = []

            def fake_aws(*args):
                calls.append(args)
                if args[:2] == ('s3', 'cp'):
                    shutil.copyfile(archive, args[-1])

            with patch.object(index, 's3', self.fake):
                with patch.object(index, 'aws_command', side_effect=fake_aws):
                    index.s3_deploy(['s3://src/site.zip'], 's3://dst/site', {},
                        {}, True, [], [], [{}], True, [{}], sign_content=True)
            self.assertTrue(any(c[:2] == ('s3', 'sync') for c in calls))
            self.assertEqual(self.fake.uploads, [])

    def test_markerful_metadata_and_nonextract_paths_stay_on_cli(self):
        self.fake.add_zip('src', 'site.zip', {'app.js': b'hello'})
        from shutil import copyfile
        with patch.object(index, 's3', self.fake):
            calls = []
            def fake_aws(*args):
                calls.append(args)
                if args[:2] == ('s3', 'cp'):
                    copyfile(next(iter(self._source_files.values())), args[-1])

            # Archive staging for the substitution-enabled path.
            with tempfile.TemporaryDirectory() as temp:
                local = os.path.join(temp, 'src.zip')
                with open(local, 'wb') as f:
                    f.write(self.fake.sources[('src', 'site.zip')])
                self._source_files = {'archive': local}
                with patch.object(index, 'aws_command', side_effect=fake_aws):
                    index.s3_deploy(['s3://src/site.zip'], 's3://dst/site', {}, {},
                        True, [], [], [{'MISSING': 'none'}], True, [{}])
                self.assertTrue(any(c[:2] == ('s3', 'sync') for c in calls))
                self.assertEqual(self.fake.uploads, [])


if __name__ == '__main__':
    unittest.main()
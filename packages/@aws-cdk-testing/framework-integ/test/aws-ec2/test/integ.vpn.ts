import * as cdk from 'aws-cdk-lib';
import { SecretValue } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import { EC2_RESTRICT_DEFAULT_SECURITY_GROUP } from 'aws-cdk-lib/cx-api';

const app = new cdk.App();
const stack = new cdk.Stack(app, 'aws-cdk-ec2-vpn');
stack.node.setContext(EC2_RESTRICT_DEFAULT_SECURITY_GROUP, false);

const tunnelLogGroup = new logs.LogGroup(stack, 'VpnTunnelLogs');

const vpc = new ec2.Vpc(stack, 'MyVpc', {
  ipAddresses: ec2.IpAddresses.cidr('10.10.0.0/16'),
  vpnConnections: {
    Dynamic: { // Dynamic routing
      ip: '52.85.255.164',
      tunnelOptions: [
        {
          preSharedKeySecret: SecretValue.unsafePlainText('secretkey1234'),
        },
      ],
    },
  },
});

vpc.addVpnConnection('Static', { // Static routing
  ip: '52.85.255.197',
  staticRoutes: [
    '192.168.10.0/24',
    '192.168.20.0/24',
  ],
});

vpc.addVpnConnection('ConfiguredTunnel', {
  ip: '52.85.255.165',
  tunnelOptions: [{
    preSharedKeySecret: SecretValue.unsafePlainText('anothersecret5678'),
    ikeVersions: ['ikev2'],
    phase1EncryptionAlgorithms: ['AES256'],
    phase2EncryptionAlgorithms: ['AES256'],
    phase1DhGroupNumbers: [14],
    phase2DhGroupNumbers: [14],
    phase1Lifetime: cdk.Duration.hours(8),
    phase2Lifetime: cdk.Duration.hours(1),
    tunnelLogs: { logGroup: tunnelLogGroup },
    bgpLogs: {
      logGroup: tunnelLogGroup,
      outputFormat: ec2.VpnTunnelLogOutputFormat.TEXT,
    },
  }],
});

app.synth();

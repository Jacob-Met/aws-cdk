import { testDeprecated } from '@aws-cdk/cdk-build-tools';
import { Match, Template } from '../../assertions';
import { LogGroup } from '../../aws-logs';
import { Duration, SecretValue, Stack, Token } from '../../core';
import { PublicSubnet, Vpc, VpnConnection, VpnTunnelLogOutputFormat } from '../lib';

describe('vpn', () => {
  test('can add a vpn connection to a vpc with a vpn gateway', () => {
    // GIVEN
    const stack = new Stack();

    // WHEN
    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          asn: 65001,
          ip: '192.0.2.1',
        },
      },
    });

    // THEN
    Template.fromStack(stack).hasResourceProperties('AWS::EC2::CustomerGateway', {
      BgpAsn: 65001,
      IpAddress: '192.0.2.1',
      Type: 'ipsec.1',
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      CustomerGatewayId: {
        Ref: 'VpcNetworkVpnConnectionCustomerGateway8B56D9AF',
      },
      Type: 'ipsec.1',
      VpnGatewayId: {
        Ref: 'VpcNetworkVpnGateway501295FA',
      },
      StaticRoutesOnly: false,
    });
  });

  test('with static routing', () => {
    // GIVEN
    const stack = new Stack();

    // WHEN
    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        static: {
          ip: '192.0.2.1',
          staticRoutes: [
            '192.168.10.0/24',
            '192.168.20.0/24',
          ],
        },
      },
    });

    // THEN
    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      CustomerGatewayId: {
        Ref: 'VpcNetworkstaticCustomerGatewayAF2651CC',
      },
      Type: 'ipsec.1',
      VpnGatewayId: {
        Ref: 'VpcNetworkVpnGateway501295FA',
      },
      StaticRoutesOnly: true,
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnectionRoute', {
      DestinationCidrBlock: '192.168.10.0/24',
      VpnConnectionId: {
        Ref: 'VpcNetworkstaticE33EA98C',
      },
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnectionRoute', {
      DestinationCidrBlock: '192.168.20.0/24',
      VpnConnectionId: {
        Ref: 'VpcNetworkstaticE33EA98C',
      },
    });
  });

  test('with tunnel options, using secret value', () => {
    // GIVEN
    const stack = new Stack();

    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [{
            preSharedKeySecret: SecretValue.unsafePlainText('secretkey1234'),
            tunnelInsideCidr: '169.254.10.0/30',
          }],
        },
      },
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      CustomerGatewayId: {
        Ref: 'VpcNetworkVpnConnectionCustomerGateway8B56D9AF',
      },
      Type: 'ipsec.1',
      VpnGatewayId: {
        Ref: 'VpcNetworkVpnGateway501295FA',
      },
      StaticRoutesOnly: false,
      VpnTunnelOptionsSpecifications: [
        {
          PreSharedKey: 'secretkey1234',
          TunnelInsideCidr: '169.254.10.0/30',
          LogOptions: Match.absent(),
        },
      ],
    });
  });

  test('configures both VPN tunnel negotiation phases', () => {
    const stack = new Stack();
    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        Connection: {
          ip: '192.0.2.1',
          tunnelOptions: [{
            ikeVersions: ['ikev2'],
            phase1EncryptionAlgorithms: ['AES256'],
            phase2EncryptionAlgorithms: ['AES256'],
            phase1IntegrityAlgorithms: ['SHA2-256'],
            phase2IntegrityAlgorithms: ['SHA2-256'],
            phase1DhGroupNumbers: [14, 19],
            phase2DhGroupNumbers: [14, 19],
            phase1Lifetime: Duration.hours(8),
            phase2Lifetime: Duration.hours(1),
            dpdTimeout: Duration.seconds(40),
            dpdTimeoutAction: 'restart',
            rekeyFuzzPercentage: 75,
            rekeyMarginTime: Duration.minutes(5),
            replayWindowSize: 128,
            startupAction: 'start',
            enableTunnelLifecycleControl: true,
          }],
        },
      },
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      VpnTunnelOptionsSpecifications: [{
        IKEVersions: [{ Value: 'ikev2' }],
        Phase1EncryptionAlgorithms: [{ Value: 'AES256' }],
        Phase2EncryptionAlgorithms: [{ Value: 'AES256' }],
        Phase1IntegrityAlgorithms: [{ Value: 'SHA2-256' }],
        Phase2IntegrityAlgorithms: [{ Value: 'SHA2-256' }],
        Phase1DHGroupNumbers: [{ Value: 14 }, { Value: 19 }],
        Phase2DHGroupNumbers: [{ Value: 14 }, { Value: 19 }],
        Phase1LifetimeSeconds: 28800,
        Phase2LifetimeSeconds: 3600,
        DPDTimeoutSeconds: 40,
        DPDTimeoutAction: 'restart',
        RekeyFuzzPercentage: 75,
        RekeyMarginTimeSeconds: 300,
        ReplayWindowSize: 128,
        StartupAction: 'start',
        EnableTunnelLifecycleControl: true,
      }],
    });
  });

  test('configures activity and BGP logs independently for each VPN tunnel', () => {
    const stack = new Stack();
    const tunnelOneLogs = new LogGroup(stack, 'TunnelOneLogs');
    const bgpOneLogs = new LogGroup(stack, 'BgpOneLogs');
    const tunnelTwoLogs = new LogGroup(stack, 'TunnelTwoLogs');
    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        Connection: {
          ip: '192.0.2.1',
          tunnelOptions: [{
            tunnelLogs: { logGroup: tunnelOneLogs },
            bgpLogs: { logGroup: bgpOneLogs, outputFormat: VpnTunnelLogOutputFormat.TEXT },
          }, {
            tunnelLogs: { logGroup: tunnelTwoLogs, outputFormat: VpnTunnelLogOutputFormat.TEXT },
          }],
        },
      },
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      VpnTunnelOptionsSpecifications: [
        {
          LogOptions: {
            CloudwatchLogOptions: {
              LogEnabled: true,
              LogGroupArn: { 'Fn::GetAtt': [Match.stringLikeRegexp('^TunnelOneLogs'), 'Arn'] },
              LogOutputFormat: 'json',
              BgpLogEnabled: true,
              BgpLogGroupArn: { 'Fn::GetAtt': [Match.stringLikeRegexp('^BgpOneLogs'), 'Arn'] },
              BgpLogOutputFormat: 'text',
            },
          },
        },
        {
          LogOptions: {
            CloudwatchLogOptions: {
              LogEnabled: true,
              LogGroupArn: { 'Fn::GetAtt': [Match.stringLikeRegexp('^TunnelTwoLogs'), 'Arn'] },
              LogOutputFormat: 'text',
            },
          },
        },
      ],
    });
  });

  testDeprecated('with tunnel options, using secret', () => {
    // GIVEN
    const stack = new Stack();

    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [{
            preSharedKey: 'secretkey1234',
            tunnelInsideCidr: '169.254.10.0/30',
          }],
        },
      },
    });

    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      CustomerGatewayId: {
        Ref: 'VpcNetworkVpnConnectionCustomerGateway8B56D9AF',
      },
      Type: 'ipsec.1',
      VpnGatewayId: {
        Ref: 'VpcNetworkVpnGateway501295FA',
      },
      StaticRoutesOnly: false,
      VpnTunnelOptionsSpecifications: [
        {
          PreSharedKey: 'secretkey1234',
          TunnelInsideCidr: '169.254.10.0/30',
        },
      ],
    });
  });

  test('fails when ip is invalid', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.256',
        },
      },
    })).toThrow(/`ip`.+IPv4/);
  });

  test('fails when specifying more than two tunnel options', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [
            {
              preSharedKeySecret: SecretValue.unsafePlainText('secretkey1234'),
            },
            {
              preSharedKeySecret: SecretValue.unsafePlainText('secretkey1234'),
            },
            {
              preSharedKeySecret: SecretValue.unsafePlainText('secretkey1234'),
            },
          ],
        },
      },
    })).toThrow(/two.+`tunnelOptions`/);
  });

  test('fails with duplicate tunnel inside cidr', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [
            {
              tunnelInsideCidr: '169.254.10.0/30',
            },
            {
              tunnelInsideCidr: '169.254.10.0/30',
            },
          ],
        },
      },
    })).toThrow(/`tunnelInsideCidr`.+both tunnels/);
  });

  test('with two tunnel options and no tunnelInsideCidr', () => {
    // GIVEN
    const stack = new Stack();

    // WHEN
    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [
            {
              preSharedKeySecret: SecretValue.unsafePlainText('secretkey1234'),
            },
            {
              preSharedKeySecret: SecretValue.unsafePlainText('secretkey5678'),
            },
          ],
        },
      },
    });

    // THEN
    Template.fromStack(stack).hasResourceProperties('AWS::EC2::VPNConnection', {
      CustomerGatewayId: {
        Ref: 'VpcNetworkVpnConnectionCustomerGateway8B56D9AF',
      },
      Type: 'ipsec.1',
      VpnGatewayId: {
        Ref: 'VpcNetworkVpnGateway501295FA',
      },
      StaticRoutesOnly: false,
      VpnTunnelOptionsSpecifications: [
        {
          PreSharedKey: 'secretkey1234',
        },
        {
          PreSharedKey: 'secretkey5678',
        },
      ],
    });
  });

  testDeprecated('fails when specifying an invalid pre-shared key', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [
            {
              preSharedKey: '0invalid',
            },
          ],
        },
      },
    })).toThrow(/`preSharedKey`/);
  });

  test('fails when specifying a reserved tunnel inside cidr', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [
            {
              tunnelInsideCidr: '169.254.1.0/30',
            },
          ],
        },
      },
    })).toThrow(/`tunnelInsideCidr`.+reserved/);
  });

  test('fails when specifying an invalid tunnel inside cidr', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: '192.0.2.1',
          tunnelOptions: [
            {
              tunnelInsideCidr: '169.200.10.0/30',
            },
          ],
        },
      },
    })).toThrow(/`tunnelInsideCidr`.+size/);
  });

  test('can use metricTunnelState on a vpn connection', () => {
    // GIVEN
    const stack = new Stack();

    const vpc = new Vpc(stack, 'VpcNetwork', {
      vpnGateway: true,
    });

    const vpn = vpc.addVpnConnection('Vpn', {
      ip: '192.0.2.1',
    });

    // THEN
    expect(stack.resolve(vpn.metricTunnelState())).toEqual({
      dimensions: { VpnId: { Ref: 'VpcNetworkVpnA476C58D' } },
      namespace: 'AWS/VPN',
      metricName: 'TunnelState',
      period: Duration.minutes(5),
      statistic: 'Average',
    });
  });

  test('can import a vpn connection from attributes', () => {
    const stack = new Stack();

    const vpn = VpnConnection.fromVpnConnectionAttributes(stack, 'Connection', {
      vpnId: 'idv',
      customerGatewayIp: 'ip',
      customerGatewayId: 'idc',
      customerGatewayAsn: 6500,
    });

    expect(vpn.vpnId).toEqual('idv');
    expect(vpn.customerGatewayAsn).toEqual(6500);
    expect(vpn.customerGatewayId).toEqual('idc');
    expect(vpn.customerGatewayIp).toEqual('ip');

    expect(stack.resolve(vpn.metricTunnelState())).toEqual({
      dimensions: { VpnId: 'idv' },
      namespace: 'AWS/VPN',
      metricName: 'TunnelState',
      period: Duration.minutes(5),
      statistic: 'Average',
    });

    expect(stack.resolve(vpn.metricTunnelDataIn())).toEqual({
      dimensions: { VpnId: 'idv' },
      namespace: 'AWS/VPN',
      metricName: 'TunnelDataIn',
      period: Duration.minutes(5),
      statistic: 'Sum',
    });

    expect(stack.resolve(vpn.metricTunnelDataOut())).toEqual({
      dimensions: { VpnId: 'idv' },
      namespace: 'AWS/VPN',
      metricName: 'TunnelDataOut',
      period: Duration.minutes(5),
      statistic: 'Sum',
    });
  });

  test('can use metricAllTunnelDataOut', () => {
    // GIVEN
    const stack = new Stack();

    // THEN
    expect(stack.resolve(VpnConnection.metricAllTunnelDataOut())).toEqual({
      namespace: 'AWS/VPN',
      metricName: 'TunnelDataOut',
      period: Duration.minutes(5),
      statistic: 'Sum',
    });
  });

  test('fails when enabling vpnGateway without having subnets', () => {
    // GIVEN
    const stack = new Stack();

    expect(() => new Vpc(stack, 'VpcNetwork', {
      vpnGateway: true,
      subnetConfiguration: [],
    })).toThrow(/VPN gateway/);
  });

  test('can add a vpn connection later to a vpc that initially had no subnets', () => {
    // GIVEN
    const stack = new Stack();

    // WHEN
    const vpc = new Vpc(stack, 'VpcNetwork', {
      subnetConfiguration: [],
    });
    const subnet = new PublicSubnet(stack, 'Subnet', {
      vpcId: vpc.vpcId,
      availabilityZone: 'eu-central-1a',
      cidrBlock: '10.0.0.0/28',
    });
    vpc.publicSubnets.push(subnet);
    vpc.addVpnConnection('VPNConnection', {
      ip: '1.2.3.4',
    });

    // THEN
    Template.fromStack(stack).hasResourceProperties('AWS::EC2::CustomerGateway', {
      Type: 'ipsec.1',
    });
  });
  test('can add a vpn connection with a Token as customer gateway ip', () => {
    // GIVEN
    const stack = new Stack();
    const token = Token.asAny('192.0.2.1');

    // WHEN
    new Vpc(stack, 'VpcNetwork', {
      vpnConnections: {
        VpnConnection: {
          ip: token as any,
        },
      },
    });

    // THEN
    Template.fromStack(stack).hasResourceProperties('AWS::EC2::CustomerGateway', {
      IpAddress: '192.0.2.1',
    });
  });
});

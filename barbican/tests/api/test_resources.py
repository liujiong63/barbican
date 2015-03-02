# Copyright (c) 2013-2014 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This test module focuses on typical-flow business logic tests with the API
resource classes. For RBAC tests of these classes, see the
'resources_policy_test.py' module.
"""
import base64
import logging
import mimetypes
import urllib

import mock
import pecan
from six import moves
import webtest

from barbican import api
from barbican.api import app
from barbican.api import controllers
from barbican.common import exception as excep
from barbican.common import hrefs
from barbican.common import validators
import barbican.context
from barbican.model import models
from barbican.openstack.common import timeutils
from barbican.tests import utils


LOG = logging.getLogger(__name__)


def get_barbican_env(external_project_id):
    """Create and return a barbican.context for use with the RBAC decorator

    Injects the provided external_project_id.
    """
    kwargs = {'roles': None,
              'user': None,
              'project': external_project_id,
              'is_admin': True}
    ctx = barbican.context.RequestContext(**kwargs)
    ctx.policy_enforcer = None
    barbican_env = {'barbican.context': ctx}
    return barbican_env


def create_secret(id_ref="id", name="name",
                  algorithm=None, bit_length=None,
                  mode=None, encrypted_datum=None, content_type=None):
    """Generate a Secret entity instance."""
    info = {'id': id_ref,
            'name': name,
            'algorithm': algorithm,
            'bit_length': bit_length,
            'mode': mode}
    secret = models.Secret(info)
    secret.id = id_ref
    if encrypted_datum:
        secret.encrypted_data = [encrypted_datum]
    if content_type:
        content_meta = models.SecretStoreMetadatum('content_type',
                                                   content_type)
        secret.secret_store_metadata['content_type'] = content_meta
    return secret


def create_order_with_meta(id_ref="id", order_type="certificate", meta={},
                           status='PENDING'):
    """Generate an Order entity instance with Metadata."""
    order = models.Order()
    order.id = id_ref
    order.type = order_type
    order.meta = meta
    order.status = status
    return order


def validate_datum(test, datum):
    test.assertIsNone(datum.kek_meta_extended)
    test.assertIsNotNone(datum.kek_meta_project)
    test.assertTrue(datum.kek_meta_project.bind_completed)
    test.assertIsNotNone(datum.kek_meta_project.plugin_name)
    test.assertIsNotNone(datum.kek_meta_project.kek_label)


def create_container(id_ref):
    """Generate a Container entity instance."""
    container = models.Container()
    container.id = id_ref
    container.name = 'test name'
    container.type = 'rsa'
    container_secret = models.ContainerSecret()
    container_secret.container_id = id
    container_secret.secret_id = '123'
    container.container_secrets.append(container_secret)
    return container


def create_consumer(container_id, id_ref):
    """Generate a ContainerConsumerMetadatum entity instance."""
    data = {
        'name': 'test name',
        'URL': 'http://test/url'
    }
    consumer = models.ContainerConsumerMetadatum(container_id, data)
    consumer.id = id_ref
    return consumer


class SecretAllowAllMimeTypesDecoratorTest(utils.BaseTestCase):

    def setUp(self):
        super(SecretAllowAllMimeTypesDecoratorTest, self).setUp()
        self.mimetype_values = set(mimetypes.types_map.values())

    @pecan.expose(generic=True)
    @controllers.secrets.allow_all_content_types
    def _empty_pecan_exposed_function(self):
        pass

    def _empty_function(self):
        pass

    def test_mimetypes_successfully_added_to_mocked_function(self):
        empty_function = mock.MagicMock()
        empty_function._pecan = {}
        func = controllers.secrets.allow_all_content_types(empty_function)
        cfg = func._pecan
        self.assertEqual(len(self.mimetype_values), len(cfg['content_types']))

    def test_mimetypes_successfully_added_to_pecan_exposed_function(self):
        cfg = self._empty_pecan_exposed_function._pecan
        self.assertEqual(len(self.mimetype_values), len(cfg['content_types']))

    def test_decorator_raises_if_function_not_pecan_exposed(self):
        self.assertRaises(AttributeError,
                          controllers.secrets.allow_all_content_types,
                          self._empty_function)


class FunctionalTest(utils.BaseTestCase, utils.MockModelRepositoryMixin):

    def setUp(self):
        super(FunctionalTest, self).setUp()
        root = self.root
        config = {'app': {'root': root}}
        pecan.set_config(config, overwrite=True)
        self.app = webtest.TestApp(pecan.make_app(root))

    def tearDown(self):
        super(FunctionalTest, self).tearDown()
        pecan.set_config({}, overwrite=True)

    @property
    def root(self):
        return controllers.versions.VersionController()


class WhenTestingVersionResource(FunctionalTest):

    def test_should_return_200_on_get(self):
        resp = self.app.get('/')
        self.assertEqual(200, resp.status_int)

    def test_should_return_version_json(self):
        resp = self.app.get('/')

        self.assertTrue('v1' in resp.json)
        self.assertEqual('current', resp.json['v1'])


class BaseSecretsResource(FunctionalTest):
    """Base test class for the Secrets resource."""

    def setUp(self):
        super(BaseSecretsResource, self).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            secrets = controllers.secrets.SecretsController()

        return RootController()

    def _init(self, payload=b'not-encrypted',
              payload_content_type='text/plain',
              payload_content_encoding=None):
        self.name = 'name'
        self.payload = payload
        self.payload_content_type = payload_content_type
        self.payload_content_encoding = payload_content_encoding
        self.secret_algorithm = 'AES'
        self.secret_bit_length = 256
        self.secret_mode = 'CBC'
        self.secret_req = {'name': self.name,
                           'algorithm': self.secret_algorithm,
                           'bit_length': self.secret_bit_length,
                           'mode': self.secret_mode}
        if payload:
            self.secret_req['payload'] = payload
        if payload_content_type:
            self.secret_req['payload_content_type'] = payload_content_type
        if payload_content_encoding:
            self.secret_req['payload_content_encoding'] = (
                payload_content_encoding)

        # Set up mocked project
        self.external_project_id = 'keystone1234'
        self.project_entity_id = 'tid1234'
        self.project = models.Project()
        self.project.id = self.project_entity_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.find_by_external_project_id.return_value = (
            self.project)
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked secret
        self.secret = models.Secret()
        self.secret.id = '123'

        # Set up mocked secret repo
        self.secret_repo = mock.MagicMock()
        self.secret_repo.create_from.return_value = self.secret
        self.setup_secret_repository_mock(self.secret_repo)

        # Set up mocked project-secret repo
        self.project_secret_repo = mock.MagicMock()
        self.project_secret_repo.create_from.return_value = None
        self.setup_project_secret_repository_mock(self.project_secret_repo)

        # Set up mocked encrypted datum repo
        self.datum_repo = mock.MagicMock()
        self.datum_repo.create_from.return_value = None
        self.setup_encrypted_datum_repository_mock(self.datum_repo)

        # Set up mocked kek datum
        self.kek_datum = models.KEKDatum()
        self.kek_datum.kek_label = "kek_label"
        self.kek_datum.bind_completed = False
        self.kek_datum.algorithm = ''
        self.kek_datum.bit_length = 0
        self.kek_datum.mode = ''
        self.kek_datum.plugin_meta = ''

        # Set up mocked kek datum repo
        self.kek_repo = mock.MagicMock()
        self.kek_repo.find_or_create_kek_datum.return_value = self.kek_datum
        self.setup_kek_datum_repository_mock(self.kek_repo)

        # Set up mocked secret meta repo
        self.setup_secret_meta_repository_mock()

        # Set up mocked transport key
        self.transport_key = models.TransportKey(
            'default_plugin_name', 'XXXABCDEF')
        self.transport_key_id = 'tkey12345'
        self.tkey_url = hrefs.convert_transport_key_to_href(
            self.transport_key.id)

        # Set up mocked transport key
        self.setup_transport_key_repository_mock()


class BaseSecretTestSuite(BaseSecretsResource):

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_add_new_secret_with_expiration(self, mock_store_secret):
        mock_store_secret.return_value = self.secret, None

        expiration = '2114-02-28 12:14:44.180394-05:00'
        self.secret_req.update({'expiration': expiration})

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req
        )

        self.assertEqual(resp.status_int, 201)

        # Validation replaces the time.
        expected = dict(self.secret_req)
        expiration_raw = expected['expiration']
        expiration_raw = expiration_raw[:-6].replace('12', '17', 1)
        expiration_tz = timeutils.parse_isotime(expiration_raw.strip())
        expected['expiration'] = timeutils.normalize_time(expiration_tz)
        mock_store_secret.assert_called_once_with(
            self.secret_req.get('payload'),
            self.secret_req.get('payload_content_type',
                                'application/octet-stream'),
            self.secret_req.get('payload_content_encoding'),
            expected,
            None,
            self.project,
            transport_key_needed=False,
            transport_key_id=None
        )

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_add_new_secret_one_step(self, mock_store_secret,
                                            check_project_id=True):
        """Test the one-step secret creation.

        :param check_project_id: True if the retrieved Project id needs to be
                                 verified, False to skip this check (necessary
                                 for new-Project flows).
        """
        mock_store_secret.return_value = self.secret, None

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req
        )
        self.assertEqual(resp.status_int, 201)

        expected = dict(self.secret_req)
        expected['expiration'] = None
        mock_store_secret.assert_called_once_with(
            self.secret_req.get('payload'),
            self.secret_req.get('payload_content_type',
                                'application/octet-stream'),
            self.secret_req.get('payload_content_encoding'),
            expected,
            None,
            self.project if check_project_id else mock.ANY,
            transport_key_needed=False,
            transport_key_id=None
        )

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_add_new_secret_one_step_with_tkey_id(
            self, mock_store_secret, check_project_id=True):
        """Test the one-step secret creation with transport_key_id set

        :param check_project_id: True if the retrieved Project id needs to be
                                 verified, False to skip this check (necessary
                                 for new-Project flows).
        """
        mock_store_secret.return_value = self.secret, None
        self.secret_req['transport_key_id'] = self.transport_key_id

        resp = self.app.post_json('/secrets/', self.secret_req)
        self.assertEqual(resp.status_int, 201)

        expected = dict(self.secret_req)
        expected['expiration'] = None
        mock_store_secret.assert_called_once_with(
            self.secret_req.get('payload'),
            self.secret_req.get('payload_content_type',
                                'application/octet-stream'),
            self.secret_req.get('payload_content_encoding'),
            expected,
            None,
            self.project if check_project_id else mock.ANY,
            transport_key_needed=False,
            transport_key_id=self.transport_key_id
        )

    def test_should_add_new_secret_if_project_does_not_exist(self):
        self.project_repo.get.return_value = None
        self.project_repo.find_by_external_project_id.return_value = None

        self.test_should_add_new_secret_one_step(check_project_id=False)

        args, kwargs = self.project_repo.create_from.call_args
        project = args[0]
        self.assertIsInstance(project, models.Project)
        self.assertEqual(self.external_project_id, project.external_id)

    def test_should_add_new_secret_metadata_without_payload(self):
        self.app.post_json(
            '/secrets/',
            {'name': self.name}
        )

        args, kwargs = self.secret_repo.create_from.call_args
        secret = args[0]
        self.assertIsInstance(secret, models.Secret)
        self.assertEqual(secret.name, self.name)

        args, kwargs = self.project_secret_repo.create_from.call_args
        project_secret = args[0]
        self.assertIsInstance(project_secret, models.ProjectSecret)
        self.assertEqual(project_secret.project_id, self.project_entity_id)
        self.assertEqual(project_secret.secret_id, secret.id)

        self.assertFalse(self.datum_repo.create_from.called)

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_add_new_secret_metadata_with_tkey(self, mock_store_secret):

        mock_store_secret.return_value = self.secret, self.transport_key
        resp = self.app.post_json(
            '/secrets/',
            {'name': self.name,
             'transport_key_needed': 'true'}
        )

        self.assertTrue('secret_ref' in resp.json)
        self.assertTrue('transport_key_ref' in resp.json)
        self.assertEqual(resp.json['transport_key_ref'], self.tkey_url)

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_add_secret_payload_almost_too_large(self,
                                                        mock_store_secret):
        mock_store_secret.return_value = self.secret, None

        if validators.DEFAULT_MAX_SECRET_BYTES % 4:
            raise ValueError('Tests currently require max secrets divides by '
                             '4 evenly, due to base64 encoding.')

        big_text = ''.join(['A' for x
                            in moves.range(
                                validators.DEFAULT_MAX_SECRET_BYTES - 8)
                            ])

        self.secret_req = {'name': self.name,
                           'algorithm': self.secret_algorithm,
                           'bit_length': self.secret_bit_length,
                           'mode': self.secret_mode,
                           'payload': big_text,
                           'payload_content_type': self.payload_content_type}

        payload_encoding = self.payload_content_encoding
        if payload_encoding:
            self.secret_req['payload_content_encoding'] = payload_encoding
        self.app.post_json('/secrets/', self.secret_req)

    def test_should_raise_due_to_payload_too_large(self):
        big_text = ''.join(['A' for x
                            in moves.range(
                                validators.DEFAULT_MAX_SECRET_BYTES + 10)
                            ])

        self.secret_req = {'name': self.name,
                           'algorithm': self.secret_algorithm,
                           'bit_length': self.secret_bit_length,
                           'mode': self.secret_mode,
                           'payload': big_text,
                           'payload_content_type': self.payload_content_type}

        payload_encoding = self.payload_content_encoding
        if payload_encoding:
            self.secret_req['payload_content_encoding'] = payload_encoding

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 413)

    def test_should_raise_due_to_empty_payload(self):
        self.secret_req = {'name': self.name,
                           'algorithm': self.secret_algorithm,
                           'bit_length': self.secret_bit_length,
                           'mode': self.secret_mode,
                           'payload': ''}

        payload_type = self.payload_content_type
        payload_encoding = self.payload_content_encoding
        if payload_type:
            self.secret_req['payload_content_type'] = payload_type
        if payload_encoding:
            self.secret_req['payload_content_encoding'] = payload_encoding

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)


class WhenCreatingPlainTextSecretsUsingSecretsResource(BaseSecretTestSuite):

    def test_should_raise_due_to_unsupported_payload_content_type(self):
        self.secret_req = {'name': self.name,
                           'payload_content_type': 'somethingbogushere',
                           'algorithm': self.secret_algorithm,
                           'bit_length': self.secret_bit_length,
                           'mode': self.secret_mode,
                           'payload': self.payload}

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)


class WhenCreatingBinarySecretsUsingSecretsResource(BaseSecretTestSuite):

    @property
    def root(self):
        self._init(payload="...lOtfqHaUUpe6NqLABgquYQ==",
                   payload_content_type='application/octet-stream',
                   payload_content_encoding='base64')
        return super(WhenCreatingBinarySecretsUsingSecretsResource, self).root

    def test_create_secret_fails_with_binary_payload_no_encoding(self):
        self.secret_req = {
            'name': self.name,
            'algorithm': self.secret_algorithm,
            'bit_length': self.secret_bit_length,
            'mode': self.secret_mode,
            'payload': 'lOtfqHaUUpe6NqLABgquYQ==',
            'payload_content_type': 'application/octet-stream'
        }
        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_create_secret_fails_with_binary_payload_bad_encoding(self):
        self.secret_req = {
            'name': self.name,
            'algorithm': self.secret_algorithm,
            'bit_length': self.secret_bit_length,
            'mode': self.secret_mode,
            'payload': 'lOtfqHaUUpe6NqLABgquYQ==',
            'payload_content_type': 'application/octet-stream',
            'payload_content_encoding': 'bogus64'
        }

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_create_secret_fails_with_binary_payload_no_content_type(self):
        self.secret_req = {
            'name': self.name,
            'algorithm': self.secret_algorithm,
            'bit_length': self.secret_bit_length,
            'mode': self.secret_mode,
            'payload': 'lOtfqHaUUpe6NqLABgquYQ==',
            'payload_content_encoding': 'base64'
        }

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_create_secret_fails_with_bad_payload(self):
        self.secret_req = {
            'name': self.name,
            'algorithm': self.secret_algorithm,
            'bit_length': self.secret_bit_length,
            'mode': self.secret_mode,
            'payload': 'AAAAAAAAA',
            'payload_content_type': 'application/octet-stream',
            'payload_content_encoding': 'base64'
        }

        resp = self.app.post_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)


class WhenGettingSecretsListUsingSecretsResource(FunctionalTest):

    def setUp(self):
        super(WhenGettingSecretsListUsingSecretsResource, self).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            secrets = controllers.secrets.SecretsController()

        return RootController()

    def _init(self):
        self.project_id = 'project1234'
        self.external_project_id = 'keystone1234'
        self.name = 'name 1234 !@#$%^&*()_+=-{}[];:<>,./?'
        self.secret_algorithm = "AES"
        self.secret_bit_length = 256
        self.secret_mode = "CBC"

        self.num_secrets = 10
        self.offset = 2
        self.limit = 2

        # Set up mocked secrets list
        secret_params = {'name': self.name,
                         'algorithm': self.secret_algorithm,
                         'bit_length': self.secret_bit_length,
                         'mode': self.secret_mode,
                         'encrypted_datum': None}

        self.secrets = [create_secret(id_ref='id' + str(id),
                                      **secret_params) for
                        id in moves.range(self.num_secrets)]
        self.total = len(self.secrets)

        # Set up mocked secret repo
        self.secret_repo = mock.MagicMock()
        self.secret_repo.get_by_create_date.return_value = (self.secrets,
                                                            self.offset,
                                                            self.limit,
                                                            self.total)
        self.setup_secret_repository_mock(self.secret_repo)

        # Set up mocked project repo
        self.setup_project_repository_mock()

        # Set up mocked project-secret repo
        self.project_secret_repo = mock.MagicMock()
        self.project_secret_repo.create_from.return_value = None
        self.setup_project_secret_repository_mock(self.project_secret_repo)

        # Set up mocked encrypted datum repo
        self.datum_repo = mock.MagicMock()
        self.datum_repo.create_from.return_value = None
        self.setup_encrypted_datum_repository_mock(self.datum_repo)

        # Set up mocked kek datum repo
        self.setup_kek_datum_repository_mock()

        # Set up mocked secret meta repo
        self.setup_secret_meta_repository_mock()

        # Set up mocked transport key repo
        self.setup_transport_key_repository_mock()

        self.params = {'offset': self.offset,
                       'limit': self.limit,
                       'name': None,
                       'alg': None,
                       'bits': 0,
                       'mode': None}

    def test_should_list_secrets_by_name(self):
        # Quote the name parameter to simulate how it would be
        # received in practice via a REST-ful GET query.
        self.params['name'] = urllib.quote_plus(self.name)

        resp = self.app.get(
            '/secrets/',
            {k: v for k, v in self.params.items() if v is not None}
        )
        # Verify that the name is unquoted correctly in the
        # secrets.on_get function prior to searching the repo.
        self.secret_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True,
            name=self.name,
            alg=None,
            mode=None,
            bits=0
        )

        self.assertIn('secrets', resp.namespace)
        secrets = resp.namespace['secrets']
        # The result should be the unquoted name
        self.assertEqual(secrets[0]['name'], self.name)

    def test_should_get_list_secrets(self):
        resp = self.app.get(
            '/secrets/',
            {k: v for k, v in self.params.items() if v is not None}
        )

        self.secret_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True,
            name='',
            alg=None,
            mode=None,
            bits=0
        )

        self.assertTrue('previous' in resp.namespace)
        self.assertTrue('next' in resp.namespace)

        url_nav_next = self._create_url(self.external_project_id,
                                        self.offset + self.limit, self.limit)
        self.assertTrue(resp.body.count(url_nav_next) == 1)

        url_nav_prev = self._create_url(self.external_project_id,
                                        0, self.limit)
        self.assertTrue(resp.body.count(url_nav_prev) == 1)

        url_hrefs = self._create_url(self.external_project_id)
        self.assertTrue(resp.body.count(url_hrefs) ==
                        (self.num_secrets + 2))

    def test_response_should_include_total(self):
        resp = self.app.get(
            '/secrets/',
            {k: v for k, v in self.params.items() if v is not None}
        )

        self.assertIn('total', resp.namespace)
        self.assertEqual(resp.namespace['total'], self.total)

    def test_should_handle_no_secrets(self):

        del self.secrets[:]

        resp = self.app.get(
            '/secrets/',
            {k: v for k, v in self.params.items() if v is not None}
        )

        self.secret_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True,
            name='',
            alg=None,
            mode=None,
            bits=0
        )

        self.assertFalse('previous' in resp.namespace)
        self.assertFalse('next' in resp.namespace)

    def test_should_list_secrets_normalize_bits_to_zero(self):
        # Set bits to a bogus value, that the secrets controller should clean.
        self.params['bits'] = 'bogus-bits'

        self.app.get(
            '/secrets/',
            {k: v for k, v in self.params.items() if v is not None}
        )

        # Verify that controller call above normalizes bits to zero!
        self.secret_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True,
            name='',
            alg=None,
            mode=None,
            bits=0
        )

    def _create_url(self, external_project_id, offset_arg=None,
                    limit_arg=None):
        if limit_arg:
            offset = int(offset_arg)
            limit = int(limit_arg)
            return '/secrets?limit={0}&offset={1}'.format(limit,
                                                          offset)
        else:
            return '/secrets'


class WhenGettingPuttingOrDeletingSecretUsingSecretResource(FunctionalTest):
    def setUp(self):
        super(
            WhenGettingPuttingOrDeletingSecretUsingSecretResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            secrets = controllers.secrets.SecretsController()

        return RootController()

    def _init(self):
        self.project_id = 'projectid1234'
        self.external_project_id = 'keystone1234'
        self.name = 'name1234'

        secret_id = utils.generate_test_uuid(tail_value=1)
        datum_id = "iddatum1"
        kek_id = "idkek1"

        self.secret_algorithm = "AES"
        self.secret_bit_length = 256
        self.secret_mode = "CBC"

        self.kek_project = models.KEKDatum()
        self.kek_project.id = kek_id
        self.kek_project.active = True
        self.kek_project.bind_completed = False
        self.kek_project.kek_label = "kek_label"

        self.datum = models.EncryptedDatum()
        self.datum.id = datum_id
        self.datum.secret_id = secret_id
        self.datum.kek_id = kek_id
        self.datum.kek_meta_project = self.kek_project
        self.datum.content_type = "text/plain"
        self.datum.cypher_text = "aaaa"  # base64 value.

        self.secret = create_secret(id_ref=secret_id,
                                    name=self.name,
                                    algorithm=self.secret_algorithm,
                                    bit_length=self.secret_bit_length,
                                    mode=self.secret_mode,
                                    encrypted_datum=self.datum,
                                    content_type=self.datum.content_type)

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.project_repo.find_by_external_project_id.return_value = (
            self.project)
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked secret repo
        self.secret_repo = mock.Mock()
        self.secret_repo.get = mock.Mock(return_value=self.secret)
        self.secret_repo.delete_entity_by_id = mock.Mock(return_value=None)
        self.setup_secret_repository_mock(self.secret_repo)

        # Set up mocked project-secret repo
        self.setup_project_secret_repository_mock()

        # Set up mocked encrypted datum repo
        self.datum_repo = mock.MagicMock()
        self.datum_repo.create_from.return_value = None
        self.setup_encrypted_datum_repository_mock(self.datum_repo)

        # Set up mocked kek datum repo
        self.setup_kek_datum_repository_mock()

        # Set up mocked secret meta repo
        self.secret_meta_repo = mock.MagicMock()
        self.secret_meta_repo.get_metadata_for_secret.return_value = None
        self.setup_secret_meta_repository_mock(self.secret_meta_repo)

        # Set up mocked transport key
        self.transport_key_model = models.TransportKey(
            "default_plugin", "my transport key")

        # Set up mocked transport key repo
        self.transport_key_repo = mock.MagicMock()
        self.transport_key_repo.get.return_value = self.transport_key_model
        self.setup_transport_key_repository_mock(self.transport_key_repo)

        self.transport_key_id = 'tkey12345'

    @mock.patch('barbican.plugin.resources.get_transport_key_id_for_retrieval')
    def test_should_get_secret_as_json(self, mock_get_transport_key):
        mock_get_transport_key.return_value = None
        resp = self.app.get(
            '/secrets/{0}/'.format(self.secret.id),
            headers={'Accept': 'application/json', 'Accept-Encoding': 'gzip'}
        )
        self.secret_repo.get.assert_called_once_with(
            entity_id=self.secret.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)
        self.assertEqual(resp.status_int, 200)

        self.assertNotIn('content_encodings', resp.namespace)
        self.assertIn('content_types', resp.namespace)
        self.assertIn(self.datum.content_type,
                      resp.namespace['content_types'].itervalues())
        self.assertNotIn('mime_type', resp.namespace)

    @mock.patch('barbican.plugin.resources.get_secret')
    def test_should_get_secret_as_plain(self, mock_get_secret):
        data = 'unencrypted_data'
        mock_get_secret.return_value = data

        resp = self.app.get(
            '/secrets/{0}/'.format(self.secret.id),
            headers={'Accept': 'text/plain'}
        )

        self.secret_repo.get.assert_called_once_with(
            entity_id=self.secret.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)
        self.assertEqual(resp.status_int, 200)

        self.assertEqual(resp.body, data)
        mock_get_secret.assert_called_once_with(
            'text/plain',
            self.secret,
            self.project,
            None,
            None
        )

    @mock.patch('barbican.plugin.resources.get_secret')
    def test_should_get_secret_as_plain_with_twsk(self, mock_get_secret):
        data = 'encrypted_data'
        mock_get_secret.return_value = data

        twsk = "trans_wrapped_session_key"
        resp = self.app.get(
            '/secrets/{0}/?trans_wrapped_session_key={1}&transport_key_id={2}'
            .format(self.secret.id, twsk, self.transport_key_id),
            headers={'Accept': 'text/plain'}
        )

        self.secret_repo.get.assert_called_once_with(
            entity_id=self.secret.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)
        self.assertEqual(resp.status_int, 200)

        self.assertEqual(resp.body, data)
        mock_get_secret.assert_called_once_with(
            'text/plain',
            self.secret,
            self.project,
            twsk,
            self.transport_key_model.transport_key
        )

    @mock.patch('barbican.plugin.resources.get_secret')
    def test_should_throw_exception_for_get_when_twsk_but_no_tkey_id(
            self, mock_get_secret):
        data = 'encrypted_data'
        mock_get_secret.return_value = data

        twsk = "trans_wrapped_session_key"
        resp = self.app.get(
            '/secrets/{0}/?trans_wrapped_session_key={1}'.format(
                self.secret.id, twsk),
            headers={'Accept': 'text/plain'},
            expect_errors=True
        )

        self.secret_repo.get.assert_called_once_with(
            entity_id=self.secret.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)
        self.assertEqual(resp.status_int, 400)

    @mock.patch('barbican.plugin.resources.get_transport_key_id_for_retrieval')
    def test_should_get_secret_meta_for_binary(self, mock_get_transport_key):
        mock_get_transport_key.return_value = None
        self.datum.content_type = "application/octet-stream"
        self.secret.secret_store_metadata['content_type'].value = (
            self.datum.content_type
        )
        self.datum.cypher_text = 'aaaa'

        resp = self.app.get(
            '/secrets/{0}/'.format(self.secret.id),
            headers={'Accept': 'application/json', 'Accept-Encoding': 'gzip'}
        )

        self.secret_repo.get.assert_called_once_with(
            entity_id=self.secret.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)

        self.assertEqual(resp.status_int, 200)

        self.assertIsNotNone(resp.namespace)
        self.assertIn('content_types', resp.namespace)
        self.assertIn(self.datum.content_type,
                      resp.namespace['content_types'].itervalues())

    @mock.patch('barbican.plugin.resources.get_transport_key_id_for_retrieval')
    def test_should_get_secret_meta_for_binary_with_tkey(
            self, mock_get_transport_key_id):
        mock_get_transport_key_id.return_value = self.transport_key_id
        self.datum.content_type = "application/octet-stream"
        self.secret.secret_store_metadata['content_type'].value = (
            self.datum.content_type
        )
        self.datum.cypher_text = 'aaaa'

        resp = self.app.get(
            '/secrets/{0}/?transport_key_needed=true'.format(
                self.secret.id),
            headers={'Accept': 'application/json', 'Accept-Encoding': 'gzip'}
        )

        self.secret_repo.get.assert_called_once_with(
            entity_id=self.secret.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)

        self.assertEqual(resp.status_int, 200)

        self.assertIsNotNone(resp.namespace)
        self.assertIn('content_types', resp.namespace)
        self.assertIn(self.datum.content_type,
                      resp.namespace['content_types'].itervalues())
        self.assertIn('transport_key_ref', resp.namespace)
        self.assertEqual(
            resp.namespace['transport_key_ref'],
            hrefs.convert_transport_key_to_href(
                self.transport_key_id)
        )

    @mock.patch('barbican.plugin.resources.get_secret')
    def test_should_get_secret_as_binary(self, mock_get_secret):
        data = 'unencrypted_data'
        mock_get_secret.return_value = data

        self.datum.content_type = "application/octet-stream"
        self.datum.cypher_text = 'aaaa'

        resp = self.app.get(
            '/secrets/{0}/'.format(self.secret.id),
            headers={
                'Accept': 'application/octet-stream',
                'Accept-Encoding': 'gzip'
            }
        )

        self.assertEqual(resp.body, data)

        mock_get_secret.assert_called_once_with(
            'application/octet-stream',
            self.secret,
            self.project,
            None,
            None
        )

    def test_should_throw_exception_for_get_when_secret_not_found(self):
        self.secret_repo.get = mock.Mock(return_value=None)

        resp = self.app.get(
            '/secrets/{0}/'.format(self.secret.id),
            headers={'Accept': 'application/json', 'Accept-Encoding': 'gzip'},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 404)

    def test_should_throw_exception_for_get_when_accept_not_supported(self):
        resp = self.app.get(
            '/secrets/{0}/'.format(self.secret.id),
            headers={'Accept': 'bogusaccept', 'Accept-Encoding': 'gzip'},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 406)

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_put_secret_as_plain(self, mock_store_secret):
        self.secret.encrypted_data = []

        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
        )

        self.assertEqual(resp.status_int, 204)

        mock_store_secret.assert_called_once_with(
            'plain text',
            'text/plain', None,
            self.secret.to_dict_fields(),
            self.secret,
            self.project,
            transport_key_id=None
        )

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_put_secret_as_plain_with_tkey_id(self, mock_store_secret):
        self.secret.encrypted_data = []

        resp = self.app.put(
            '/secrets/{0}/?transport_key_id={1}'.format(
                self.secret.id, self.transport_key_id),
            'plain text',
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
        )

        self.assertEqual(resp.status_int, 204)

        mock_store_secret.assert_called_once_with(
            'plain text',
            'text/plain', None,
            self.secret.to_dict_fields(),
            self.secret,
            self.project,
            transport_key_id=self.transport_key_id
        )

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_put_secret_as_binary(self, mock_store_secret):
        self.secret.encrypted_data = []

        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={
                'Accept': 'text/plain',
                'Content-Type': 'application/octet-stream'
            },
        )

        self.assertEqual(resp.status_int, 204)

        mock_store_secret.assert_called_once_with(
            'plain text',
            'application/octet-stream',
            None,
            self.secret.to_dict_fields(),
            self.secret,
            self.project,
            transport_key_id=None
        )

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_put_secret_as_binary_with_tkey_id(self, mock_store_secret):
        self.secret.encrypted_data = []

        resp = self.app.put(
            '/secrets/{0}/?transport_key_id={1}'.format(
                self.secret.id, self.transport_key_id),
            'plain text',
            headers={
                'Accept': 'text/plain',
                'Content-Type': 'application/octet-stream'
            },
        )

        self.assertEqual(resp.status_int, 204)

        mock_store_secret.assert_called_once_with(
            'plain text',
            'application/octet-stream',
            None,
            self.secret.to_dict_fields(),
            self.secret,
            self.project,
            transport_key_id=self.transport_key_id
        )

    @mock.patch('barbican.plugin.resources.store_secret')
    def test_should_put_encoded_secret_as_binary(self, mock_store_secret):
        self.secret.encrypted_data = []
        payload = base64.b64encode('plain text')
        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            payload,
            headers={
                'Accept': 'text/plain',
                'Content-Type': 'application/octet-stream',
                'Content-Encoding': 'base64'
            },
        )

        self.assertEqual(resp.status_int, 204)

        mock_store_secret.assert_called_once_with(
            payload,
            'application/octet-stream',
            'base64', self.secret.to_dict_fields(),
            self.secret,
            self.project,
            transport_key_id=None
        )

    def test_should_raise_to_put_secret_with_unsupported_encoding(self):
        self.secret.encrypted_data = []
        self.secret.secret_store_metadata.clear()
        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={
                'Accept': 'text/plain',
                'Content-Type': 'application/octet-stream',
                'Content-Encoding': 'bogusencoding'
            },
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 400)

    def test_should_raise_put_secret_as_json(self):
        self.secret.encrypted_data = []
        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={
                'Accept': 'text/plain',
                'Content-Type': 'application/json'
            },
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 415)

    def test_should_raise_put_secret_no_content_type(self):
        self.secret.encrypted_data = []
        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={
                'Accept': 'text/plain',
                'Content-Type': ''
            },
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 415)

    def test_should_raise_put_secret_not_found(self):
        # Force error, due to secret not found.
        self.secret_repo.get.return_value = None

        self.secret.encrypted_data = []
        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 404)

    def test_should_raise_put_secret_no_payload(self):
        self.secret.encrypted_data = []
        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            # response.body = None
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 400)

    def test_should_raise_put_secret_with_existing_datum(self):
        # Force error due to secret already having data
        self.secret.encrypted_data = [self.datum]

        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            'plain text',
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 409)

    def test_should_raise_due_to_empty_payload(self):
        self.secret.encrypted_data = []

        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            '',
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_raise_due_to_plain_text_too_large(self):
        big_text = ''.join(['A' for x in moves.range(
            2 * validators.DEFAULT_MAX_SECRET_BYTES)])

        self.secret.encrypted_data = []

        resp = self.app.put(
            '/secrets/{0}/'.format(self.secret.id),
            big_text,
            headers={'Accept': 'text/plain', 'Content-Type': 'text/plain'},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 413)

    @mock.patch('barbican.plugin.resources.delete_secret')
    def test_should_delete_secret(self, mock_delete_secret):
        self.app.delete(
            '/secrets/{0}/'.format(self.secret.id)
        )

        mock_delete_secret.assert_called_once_with(self.secret,
                                                   self.external_project_id)

    @mock.patch('barbican.plugin.resources.delete_secret')
    def test_should_delete_with_accept_header_application_json(
            self, mock_delete_secret):
        """Covers Launchpad Bug: 1326481."""
        self.app.delete(
            '/secrets/{0}/'.format(self.secret.id),
            headers={'Accept': 'application/json'}
        )

        mock_delete_secret.assert_called_once_with(self.secret,
                                                   self.external_project_id)

    def test_should_throw_exception_for_delete_when_secret_not_found(self):
        self.secret_repo.get.return_value = None

        resp = self.app.delete(
            '/secrets/{0}/'.format(self.secret.id),
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 404)
        # Error response should have json content type
        self.assertEqual(resp.content_type, "application/json")


class WhenPerformingUnallowedOperationsOnSecrets(BaseSecretsResource):

    def test_should_not_allow_put_on_secrets(self):
        resp = self.app.put_json(
            '/secrets/',
            self.secret_req,
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_delete_on_secrets(self):
        resp = self.app.delete(
            '/secrets/',
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 405)


class WhenCreatingOrdersUsingOrdersResource(FunctionalTest):

    def setUp(self):
        super(
            WhenCreatingOrdersUsingOrdersResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            orders = controllers.orders.OrdersController(self.queue_resource)

        return RootController()

    def _init(self):
        self.secret_name = 'name'
        self.secret_payload_content_type = 'application/octet-stream'
        self.secret_algorithm = "aes"
        self.secret_bit_length = 128
        self.secret_mode = "cbc"

        self.project_internal_id = 'projectid1234'
        self.external_project_id = 'keystoneid1234'

        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project

        self.order_repo = mock.MagicMock()
        self.order_repo.create_from.return_value = None

        self.setup_order_repository_mock(self.order_repo)
        self.setup_project_repository_mock(self.project_repo)

        self.queue_resource = mock.MagicMock()
        self.queue_resource.process_order.return_value = None

        self.type = 'key'
        self.meta = {"name": "secretname",
                     "algorithm": "AES",
                     "bit_length": 256,
                     "mode": "cbc",
                     'payload_content_type': 'application/octet-stream'}

        self.key_order_req = {'type': self.type,
                              'meta': self.meta}

    def test_should_add_new_order(self):
        resp = self.app.post_json(
            '/orders/',
            self.key_order_req
        )
        self.assertEqual(resp.status_int, 202)

        self.queue_resource.process_type_order.assert_called_once_with(
            order_id=None, project_id=self.external_project_id)

        args, kwargs = self.order_repo.create_from.call_args
        order = args[0]
        self.assertIsInstance(order, models.Order)

    def test_should_fail_creating_order_with_bogus_content(self):
        resp = self.app.post(
            '/orders/',
            'bogus',
            headers={
                'Content-Type': 'application/json'
            },
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_allow_add_new_order_unsupported_algorithm(self):
        # TODO(john-wood-w) Allow until plugin validation is added.

        # Using unsupported algorithm field for this test
        self.unsupported_req = {
            'type': 'key',
            'meta': {
                'name': self.secret_name,
                'payload_content_type': self.secret_payload_content_type,
                'algorithm': "not supported",
                'bit_length': self.secret_bit_length,
                'mode': self.secret_mode
            }
        }
        resp = self.app.post_json(
            '/orders/',
            self.unsupported_req,
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 202)

    def test_should_raise_add_new_order_no_secret_info(self):
        resp = self.app.post_json(
            '/orders/',
            {},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_raise_add_new_order_no_type(self):
        resp = self.app.post_json(
            '/orders/',
            {'meta': self.meta},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_raise_add_new_order_no_meta(self):
        resp = self.app.post_json(
            '/orders/',
            {'type': self.type},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_raise_add_new_order_bad_json(self):
        resp = self.app.post(
            '/orders/',
            '',
            expect_errors=True,
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_raise_add_new_order_no_content_type_header(self):
        resp = self.app.post(
            '/orders/',
            self.key_order_req,
            expect_errors=True,
        )
        self.assertEqual(resp.status_int, 415)

    def test_should_raise_add_new_order_with_unsupported_content_type(self):
        self.meta["payload_content_type"] = 'unsupported type'
        resp = self.app.post_json(
            '/orders/',
            self.key_order_req,
            expect_errors=True,
        )
        self.assertEqual(resp.status_int, 400)


class WhenGettingOrdersListUsingOrdersResource(FunctionalTest):
    def setUp(self):
        super(
            WhenGettingOrdersListUsingOrdersResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            orders = controllers.orders.OrdersController(self.queue_resource)

        return RootController()

    def _init(self):
        self.project_id = 'project1234'
        self.external_project_id = 'keystoneid1234'
        self.name = 'name1234'
        self.mime_type = 'text/plain'
        self.secret_algorithm = "algo"
        self.secret_bit_length = 512
        self.secret_mode = "cytype"
        self.params = {'offset': 2, 'limit': 2}

        self.num_orders = 10
        self.offset = 2
        self.limit = 2

        order_meta = {'name': self.name,
                      'algorithm': self.secret_algorithm,
                      'bit_length': self.secret_bit_length,
                      'mode': self.secret_mode}

        self.orders = [create_order_with_meta(id_ref='id' + str(id),
                                              order_type='key',
                                              meta=order_meta) for
                       id in moves.range(self.num_orders)]
        self.total = len(self.orders)
        self.order_repo = mock.MagicMock()
        self.order_repo.get_by_create_date.return_value = (self.orders,
                                                           self.offset,
                                                           self.limit,
                                                           self.total)

        self.setup_order_repository_mock(self.order_repo)
        self.setup_project_repository_mock()

        self.queue_resource = mock.MagicMock()
        self.queue_resource.process_order.return_value = None

        self.params = {
            'offset': self.offset,
            'limit': self.limit
        }

    def test_should_get_list_orders(self):
        resp = self.app.get('/orders/', self.params)

        self.order_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True
        )

        self.assertTrue('previous' in resp.namespace)
        self.assertTrue('next' in resp.namespace)

        url_nav_next = self._create_url(self.external_project_id,
                                        self.offset + self.limit, self.limit)
        self.assertTrue(resp.body.count(url_nav_next) == 1)

        url_nav_prev = self._create_url(self.external_project_id,
                                        0, self.limit)
        self.assertTrue(resp.body.count(url_nav_prev) == 1)

        url_hrefs = self._create_url(self.external_project_id)
        self.assertTrue(resp.body.count(url_hrefs) ==
                        (self.num_orders + 2))

    def test_response_should_include_total(self):
        resp = self.app.get('/orders/', self.params)
        self.assertIn('total', resp.namespace)
        self.assertEqual(resp.namespace['total'], self.total)

    def test_should_handle_no_orders(self):

        del self.orders[:]

        resp = self.app.get('/orders/', self.params)

        self.order_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True
        )

        self.assertFalse('previous' in resp.namespace)
        self.assertFalse('next' in resp.namespace)

    def _create_url(self, external_project_id, offset_arg=None,
                    limit_arg=None):
        if limit_arg:
            offset = int(offset_arg)
            limit = int(limit_arg)
            return '/orders?limit={0}&offset={1}'.format(limit,
                                                         offset)
        else:
            return '/orders'


class WhenGettingOrDeletingOrderUsingOrderResource(FunctionalTest):
    def setUp(self):
        super(
            WhenGettingOrDeletingOrderUsingOrderResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            orders = controllers.orders.OrdersController(self.queue_resource)

        return RootController()

    def _init(self):
        self.external_project_id = 'keystoneid1234'
        self.requestor = 'requestor1234'

        self.order = create_order_with_meta(
            id_ref=utils.generate_test_uuid(tail_value=1),
            order_type='key',
            meta='{"name":"just_test"}'
        )

        self.order_repo = mock.MagicMock()
        self.order_repo.get.return_value = self.order
        self.order_repo.save.return_value = None
        self.order_repo.delete_entity_by_id.return_value = None

        self.setup_order_repository_mock(self.order_repo)
        self.setup_project_repository_mock()

        self.queue_resource = mock.MagicMock()

    def test_should_get_order(self):
        self.app.get('/orders/{0}/'.format(self.order.id))

        self.order_repo.get.assert_called_once_with(
            entity_id=self.order.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)

    def test_should_delete_order(self):
        self.app.delete('/orders/{0}/'.format(self.order.id))
        self.order_repo.delete_entity_by_id.assert_called_once_with(
            entity_id=self.order.id,
            external_project_id=self.external_project_id)

    def test_should_404_for_get_when_order_not_found(self):
        self.order_repo.get.return_value = None
        resp = self.app.get(
            '/orders/{0}/'.format(self.order.id),
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 404)

    def test_should_404_for_delete_when_order_not_found(self):
        self.order_repo.get.return_value = None
        resp = self.app.delete(
            '/orders/{0}/'.format(self.order.id),
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 404)
        # Error response should have json content type
        self.assertEqual(resp.content_type, "application/json")


class WhenPuttingOrderWithMetadataUsingOrderResource(FunctionalTest):

    def setUp(self):
        super(
            WhenPuttingOrderWithMetadataUsingOrderResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            orders = controllers.orders.OrdersController(self.queue_resource)

        return RootController()

    def _init(self):
        self.external_project_id = 'keystoneid1234'
        self.requestor = 'requestor1234'

        self.order = create_order_with_meta(
            id_ref=utils.generate_test_uuid(tail_value=1),
            order_type='certificate',
            meta={'email': 'email@email.com'},
            status="PENDING"
        )

        self.order_repo = mock.MagicMock()
        self.order_repo.get.return_value = self.order
        self.order_repo.save.return_value = None
        self.order_repo.delete_entity_by_id.return_value = None

        self.type = 'certificate'
        self.meta = {'email': 'newemail@email.com'}

        self.params = {'type': self.type, 'meta': self.meta}

        self.setup_order_repository_mock(self.order_repo)
        self.setup_project_repository_mock()

        self.queue_resource = mock.MagicMock()

    def test_should_put_order(self):
        resp = self.app.put_json(
            '/orders/{0}/'.format(self.order.id),
            self.params,
            headers={
                'Content-Type': 'application/json'
            }
        )

        self.assertEqual(resp.status_int, 204)
        self.order_repo.get.assert_called_once_with(
            entity_id=self.order.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)

    def test_should_fail_with_bogus_content(self):
        resp = self.app.put(
            '/orders/{0}/'.format(self.order.id),
            'bogus',
            headers={
                'Content-Type': 'application/json'
            },
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_fail_bad_type(self):
        self.order['type'] = 'secret'
        resp = self.app.put_json(
            '/orders/{0}/'.format(self.order.id),
            self.params,
            headers={
                'Content-Type': 'application/json'
            },
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 400)
        self.order_repo.get.assert_called_once_with(
            entity_id=self.order.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)

    def test_should_fail_bad_status(self):
        self.order['status'] = 'DONE'
        resp = self.app.put_json(
            '/orders/{0}/'.format(self.order.id),
            self.params,
            headers={
                'Content-Type': 'application/json'
            },
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 400)
        self.order_repo.get.assert_called_once_with(
            entity_id=self.order.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)


class WhenCreatingTypeOrdersUsingOrdersResource(FunctionalTest):

    def setUp(self):
        super(
            WhenCreatingTypeOrdersUsingOrdersResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            orders = controllers.orders.OrdersController(self.queue_resource)

        return RootController()

    def _init(self):
        self.type = 'key'
        self.meta = {'name': 'secretname',
                     'algorithm': 'AES',
                     'bit_length': 256,
                     'mode': 'cbc',
                     'payload_content_type':
                     'application/octet-stream'}

        self.key_order_req = {'type': self.type,
                              'meta': self.meta}

        self.project_internal_id = 'projectid1234'
        self.external_project_id = 'keystoneid1234'

        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project

        self.order_repo = mock.MagicMock()
        self.order_repo.create_from.return_value = None

        self.setup_order_repository_mock(self.order_repo)
        self.setup_project_repository_mock(self.project_repo)

        self.queue_resource = mock.MagicMock()
        self.queue_resource.process_type_order.return_value = None

    def test_should_add_new_order(self):
        resp = self.app.post_json(
            '/orders/', self.key_order_req
        )
        self.assertEqual(resp.status_int, 202)

        self.queue_resource.process_type_order.assert_called_once_with(
            order_id=None, project_id=self.external_project_id)

        args, kwargs = self.order_repo.create_from.call_args
        order = args[0]
        self.assertIsInstance(order, models.Order)

    def test_should_fail_add_new_order_no_secret_json(self):
        resp = self.app.post_json(
            '/orders/', {},
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_fail_add_new_order_bad_json(self):
        resp = self.app.post(
            '/orders/', '',
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 415)


class WhenPerformingUnallowedOperationsOnOrders(FunctionalTest):

    def setUp(self):
        super(
            WhenPerformingUnallowedOperationsOnOrders, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            orders = controllers.orders.OrdersController(self.queue_resource)

        return RootController()

    def _init(self):
        self.project_internal_id = 'projectid1234'
        self.external_project_id = 'keystoneid1234'

        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project

        self.order_repo = mock.MagicMock()
        self.order_repo.create_from.return_value = None

        self.setup_order_repository_mock(self.order_repo)
        self.setup_project_repository_mock(self.project_repo)

        self.queue_resource = mock.MagicMock()

        self.type = 'key'
        self.meta = {"name": "secretname",
                     "algorithm": "AES",
                     "bit_length": 256,
                     "mode": "cbc",
                     'payload_content_type':
                     'application/octet-stream'}

        self.key_order_req = {'type': self.type,
                              'meta': self.meta}

    def test_should_not_allow_put_orders(self):
        resp = self.app.put_json(
            '/orders/',
            self.key_order_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_delete_orders(self):
        resp = self.app.delete(
            '/orders/',
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_post_order_by_id(self):
        resp = self.app.post_json(
            '/orders/{0}/'.format(utils.generate_test_uuid(tail_value=1)),
            self.key_order_req,
            headers={
                'Content-Type': 'application/json'
            },
            expect_errors=True
        )

        self.assertEqual(resp.status_int, 405)


class WhenAddingNavigationHrefs(utils.BaseTestCase):

    def setUp(self):
        super(WhenAddingNavigationHrefs, self).setUp()

        self.resource_name = 'orders'
        self.external_project_id = '12345'
        self.num_elements = 100
        self.data = {}

    def test_add_nav_hrefs_adds_next_only(self):
        offset = 0
        limit = 10

        data_with_hrefs = hrefs.add_nav_hrefs(
            self.resource_name, offset, limit, self.num_elements, self.data)

        self.assertNotIn('previous', data_with_hrefs)
        self.assertIn('next', data_with_hrefs)

    def test_add_nav_hrefs_adds_both_next_and_previous(self):
        offset = 10
        limit = 10

        data_with_hrefs = hrefs.add_nav_hrefs(
            self.resource_name, offset, limit, self.num_elements, self.data)

        self.assertIn('previous', data_with_hrefs)
        self.assertIn('next', data_with_hrefs)

    def test_add_nav_hrefs_adds_previous_only(self):
        offset = 90
        limit = 10

        data_with_hrefs = hrefs.add_nav_hrefs(
            self.resource_name, offset, limit, self.num_elements, self.data)

        self.assertIn('previous', data_with_hrefs)
        self.assertNotIn('next', data_with_hrefs)


class TestingJsonSanitization(utils.BaseTestCase):

    def test_json_sanitization_without_array(self):
        json_without_array = {"name": "name", "algorithm": "AES",
                              "payload_content_type": "  text/plain   ",
                              "mode": "CBC", "bit_length": 256,
                              "payload": "not-encrypted"}

        self.assertTrue(json_without_array['payload_content_type']
                        .startswith(' '), "whitespace should be there")
        self.assertTrue(json_without_array['payload_content_type']
                        .endswith(' '), "whitespace should be there")
        api.strip_whitespace(json_without_array)
        self.assertFalse(json_without_array['payload_content_type']
                         .startswith(' '), "whitespace should be gone")
        self.assertFalse(json_without_array['payload_content_type']
                         .endswith(' '), "whitespace should be gone")

    def test_json_sanitization_with_array(self):
        json_with_array = {"name": "name", "algorithm": "AES",
                           "payload_content_type": "text/plain",
                           "mode": "CBC", "bit_length": 256,
                           "payload": "not-encrypted",
                           "an-array":
                           [{"name": " item 1"},
                            {"name": "item2 "}]}

        self.assertTrue(json_with_array['an-array'][0]['name']
                        .startswith(' '), "whitespace should be there")
        self.assertTrue(json_with_array['an-array'][1]['name']
                        .endswith(' '), "whitespace should be there")
        api.strip_whitespace(json_with_array)
        self.assertFalse(json_with_array['an-array'][0]['name']
                         .startswith(' '), "whitespace should be gone")
        self.assertFalse(json_with_array['an-array'][1]['name']
                         .endswith(' '), "whitespace should be gone")


class WhenCreatingContainersUsingContainersResource(FunctionalTest):

    def setUp(self):
        super(
            WhenCreatingContainersUsingContainersResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.name = 'test container name'
        self.type = 'generic'
        self.secret_refs = [
            {
                'name': 'test secret 1',
                'secret_ref': '1231'
            },
            {
                'name': 'test secret 2',
                'secret_ref': '1232'
            },
            {
                'name': 'test secret 3',
                'secret_ref': '1233'
            }
        ]

        self.project_internal_id = 'projectid1234'
        self.external_project_id = 'keystoneid1234'

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked container consumer repo
        self.container_repo = mock.MagicMock()
        self.container_repo.create_from.return_value = None
        self.setup_container_repository_mock(self.container_repo)

        # Set up mocked secret repo
        self.secret_repo = mock.MagicMock()
        self.secret_repo.create_from.return_value = None
        self.setup_secret_repository_mock(self.secret_repo)

        # Set up mocked consumer repo
        self.consumer_repo = mock.MagicMock()
        self.consumer_repo.create_from.return_value = None
        self.setup_container_consumer_repository_mock(self.consumer_repo)

        self.container_req = {'name': self.name,
                              'type': self.type,
                              'secret_refs': self.secret_refs}

    def test_should_add_new_container(self):
        resp = self.app.post_json(
            '/containers/',
            self.container_req
        )
        self.assertEqual(resp.status_int, 201)
        self.assertNotIn(self.external_project_id, resp.headers['Location'])

        args, kwargs = self.container_repo.create_from.call_args
        container = args[0]
        self.assertIsInstance(container, models.Container)

    def _assert_returned_valid_container(self, resp):
        self.assertEqual(resp.status_int, 201)
        self.assertNotIn(self.external_project_id, resp.headers['Location'])

        args, kwargs = self.container_repo.create_from.call_args
        container = args[0]
        self.assertIsInstance(container, models.Container)

    def test_should_create_container_w_empty_name(self):
        # Name key missing
        container_req_w_no_name = {'type': self.type,
                                   'secret_refs': self.secret_refs}
        resp = self.app.post_json(
            '/containers/',
            container_req_w_no_name
        )

        self._assert_returned_valid_container(resp)

        # Name key is null
        container_req_w_null_name = {'name': None,
                                     'type': self.type,
                                     'secret_refs': self.secret_refs}
        resp = self.app.post_json(
            '/containers/',
            container_req_w_null_name
        )

        self._assert_returned_valid_container(resp)

    def test_should_raise_container_bad_json(self):
        resp = self.app.post(
            '/containers/',
            '',
            expect_errors=True,
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(resp.status_int, 400)

    def test_should_raise_container_no_content_type_header(self):
        resp = self.app.post(
            '/containers/',
            self.container_req,
            expect_errors=True,
        )
        self.assertEqual(resp.status_int, 415)

    def test_should_throw_exception_when_secret_ref_doesnt_exist(self):
        self.secret_repo.get.return_value = None
        resp = self.app.post_json(
            '/containers/',
            self.container_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 404)


class WhenGettingOrDeletingContainerUsingContainerResource(FunctionalTest):

    def setUp(self):
        super(
            WhenGettingOrDeletingContainerUsingContainerResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.external_project_id = 'keystoneid1234'
        self.project_internal_id = 'projectid1234'

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked container
        self.container = create_container(id_ref='id1')

        # Set up mocked container repo
        self.container_repo = mock.MagicMock()
        self.container_repo.get.return_value = self.container
        self.container_repo.delete_entity_by_id.return_value = None
        self.setup_container_repository_mock(self.container_repo)

        # Set up mocked secret repo
        self.setup_secret_repository_mock()

        # Set up container consumer repo
        self.setup_container_consumer_repository_mock()

    def test_should_get_container(self):
        self.app.get('/containers/{0}/'.format(
            self.container.id
        ))

        self.container_repo.get.assert_called_once_with(
            entity_id=self.container.id,
            external_project_id=self.external_project_id,
            suppress_exception=True)

    def test_should_delete_container(self):
        self.app.delete('/containers/{0}/'.format(
            self.container.id
        ))

        self.container_repo.delete_entity_by_id.assert_called_once_with(
            entity_id=self.container.id,
            external_project_id=self.external_project_id)

    def test_should_throw_exception_for_get_when_container_not_found(self):
        self.container_repo.get.return_value = None
        resp = self.app.get('/containers/{0}/'.format(
            self.container.id
        ), expect_errors=True)
        self.assertEqual(resp.status_int, 404)

    def test_should_throw_exception_for_delete_when_container_not_found(self):
        self.container_repo.delete_entity_by_id.side_effect = excep.NotFound(
            "Test not found exception")

        resp = self.app.delete('/containers/{0}/'.format(
            self.container.id
        ), expect_errors=True)
        self.assertEqual(resp.status_int, 404)
        # Error response should have json content type
        self.assertEqual(resp.content_type, "application/json")


class WhenPerformingUnallowedOperationsOnContainers(FunctionalTest):
    def setUp(self):
        super(
            WhenPerformingUnallowedOperationsOnContainers, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.name = 'test container name'
        self.type = 'generic'
        self.secret_refs = [
            {
                'name': 'test secret 1',
                'secret_ref': '1231'
            },
            {
                'name': 'test secret 2',
                'secret_ref': '1232'
            },
            {
                'name': 'test secret 3',
                'secret_ref': '1233'
            }
        ]

        self.external_project_id = 'keystoneid1234'
        self.project_internal_id = 'projectid1234'

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked container
        self.container = create_container(id_ref='id1')

        # Set up mocked container repo
        self.container_repo = mock.MagicMock()
        self.container_repo.get.return_value = self.container
        self.container_repo.delete_entity_by_id.return_value = None
        self.setup_container_repository_mock(self.container_repo)

        # Set up secret repo
        self.setup_secret_repository_mock()

        # Set up container consumer repo
        self.setup_container_consumer_repository_mock()

        self.container_req = {'name': self.name,
                              'type': self.type,
                              'secret_refs': self.secret_refs}

    def test_should_not_allow_put_on_containers(self):
        resp = self.app.put_json(
            '/containers/',
            self.container_req,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_post_on_container_by_id(self):
        resp = self.app.post_json(
            '/containers/{0}/'.format(self.container.id),
            self.container_req,
            expect_errors=True)
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_put_on_container_by_id(self):
        resp = self.app.put_json(
            '/containers/{0}/'.format(self.container.id),
            self.container_req,
            expect_errors=True)
        self.assertEqual(resp.status_int, 405)


class WhenCreatingConsumersUsingConsumersResource(FunctionalTest):
    def setUp(self):
        super(
            WhenCreatingConsumersUsingConsumersResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.name = 'test container name'
        self.type = 'generic'
        self.secret_refs = [
            {
                'name': 'test secret 1',
                'secret_ref': '1231'
            },
            {
                'name': 'test secret 2',
                'secret_ref': '1232'
            },
            {
                'name': 'test secret 3',
                'secret_ref': '1233'
            }
        ]

        self.consumer_ref = {
            'name': 'test_consumer1',
            'URL': 'http://consumer/1'
        }

        self.project_internal_id = 'projectid1234'
        self.external_project_id = 'keystoneid1234'

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked container
        self.container = create_container(id_ref='id1')

        # Set up mocked container repo
        self.container_repo = mock.MagicMock()
        self.container_repo.get.return_value = self.container
        self.setup_container_repository_mock(self.container_repo)

        # Set up secret repo
        self.secret_repo = mock.MagicMock()
        self.secret_repo.create_from.return_value = None
        self.setup_secret_repository_mock(self.secret_repo)

        # Set up container consumer repo
        self.consumer_repo = mock.MagicMock()
        self.consumer_repo.create_from.return_value = None
        self.setup_container_consumer_repository_mock(self.consumer_repo)

        self.container_req = {'name': self.name,
                              'type': self.type,
                              'secret_refs': self.secret_refs}

    def test_should_add_new_consumer(self):
        resp = self.app.post_json(
            '/containers/{0}/consumers/'.format(self.container.id),
            self.consumer_ref
        )
        self.assertEqual(resp.status_int, 200)
        self.assertNotIn(self.external_project_id, resp.headers['Location'])

        args, kwargs = self.consumer_repo.create_or_update_from.call_args
        consumer = args[0]
        self.assertIsInstance(consumer, models.ContainerConsumerMetadatum)

    def test_should_fail_consumer_bad_json(self):
        resp = self.app.post(
            '/containers/{0}/consumers/'.format(self.container.id),
            '',
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 415)

    def test_should_404_consumer_bad_container_id(self):
        self.container_repo.get.side_effect = excep.NotFound()
        resp = self.app.post_json(
            '/containers/{0}/consumers/'.format('bad_id'),
            self.consumer_ref, expect_errors=True
        )
        self.container_repo.get.side_effect = None
        self.assertEqual(resp.status_int, 404)

    def test_should_raise_exception_when_container_ref_doesnt_exist(self):
        self.container_repo.get.return_value = None
        resp = self.app.post_json(
            '/containers/{0}/consumers/'.format(self.container.id),
            self.consumer_ref,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 404)


class WhenGettingOrDeletingConsumersUsingConsumerResource(FunctionalTest):

    def setUp(self):
        super(
            WhenGettingOrDeletingConsumersUsingConsumerResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.external_project_id = 'keystoneid1234'
        self.project_internal_id = 'projectid1234'

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked container
        self.container = create_container(id_ref='id1')

        # Set up mocked consumers
        self.consumer = create_consumer(self.container.id, id_ref='id2')
        self.consumer2 = create_consumer(self.container.id, id_ref='id3')

        self.consumer_ref = {
            'name': self.consumer.name,
            'URL': self.consumer.URL
        }

        # Set up mocked container repo
        self.container_repo = mock.MagicMock()
        self.container_repo.get.return_value = self.container
        self.setup_container_repository_mock(self.container_repo)

        # Set up mocked container consumer repo
        self.consumer_repo = mock.MagicMock()
        self.consumer_repo.get_by_values.return_value = self.consumer
        self.consumer_repo.delete_entity_by_id.return_value = None
        self.setup_container_consumer_repository_mock(self.consumer_repo)

        # Set up mocked secret repo
        self.setup_secret_repository_mock()

    def test_should_get_consumer(self):
        ret_val = ([self.consumer], 0, 0, 1)
        self.consumer_repo.get_by_container_id.return_value = ret_val

        resp = self.app.get('/containers/{0}/consumers/'.format(
            self.container.id
        ))
        self.assertEqual(resp.status_int, 200)

        self.consumer_repo.get_by_container_id.assert_called_once_with(
            self.container.id,
            limit_arg=None,
            offset_arg=0,
            suppress_exception=True
        )

        self.assertEqual(self.consumer.name, resp.json['consumers'][0]['name'])
        self.assertEqual(self.consumer.URL, resp.json['consumers'][0]['URL'])

    def test_should_404_with_bad_container_id(self):
        self.container_repo.get.side_effect = excep.NotFound()
        resp = self.app.get('/containers/{0}/consumers/'.format(
            'bad_id'
        ), expect_errors=True)
        self.container_repo.get.side_effect = None
        self.assertEqual(resp.status_int, 404)

    def test_should_get_consumer_by_id(self):
        self.consumer_repo.get.return_value = self.consumer
        resp = self.app.get('/containers/{0}/consumers/{1}/'.format(
            self.container.id, self.consumer.id
        ))
        self.assertEqual(resp.status_int, 200)

    def test_should_404_with_bad_consumer_id(self):
        self.consumer_repo.get.return_value = None
        resp = self.app.get('/containers/{0}/consumers/{1}/'.format(
            self.container.id, 'bad_id'
        ), expect_errors=True)
        self.assertEqual(resp.status_int, 404)

    def test_should_get_no_consumers(self):
        self.consumer_repo.get_by_container_id.return_value = ([], 0, 0, 0)
        resp = self.app.get('/containers/{0}/consumers/'.format(
            self.container.id
        ))
        self.assertEqual(resp.status_int, 200)

    def test_should_delete_consumer(self):
        self.app.delete_json('/containers/{0}/consumers/'.format(
            self.container.id
        ), self.consumer_ref)

        self.consumer_repo.delete_entity_by_id.assert_called_once_with(
            self.consumer.id, self.external_project_id)

    def test_should_fail_deleting_consumer_bad_json(self):
        resp = self.app.delete(
            '/containers/{0}/consumers/'.format(self.container.id),
            '',
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 415)

    def test_should_404_on_delete_when_consumer_not_found(self):
        old_return = self.consumer_repo.get_by_values.return_value
        self.consumer_repo.get_by_values.return_value = None
        resp = self.app.delete_json('/containers/{0}/consumers/'.format(
            self.container.id
        ), self.consumer_ref, expect_errors=True)
        self.consumer_repo.get_by_values.return_value = old_return
        self.assertEqual(resp.status_int, 404)
        # Error response should have json content type
        self.assertEqual(resp.content_type, "application/json")

    def test_should_404_on_delete_when_consumer_not_found_later(self):
        self.consumer_repo.delete_entity_by_id.side_effect = excep.NotFound()
        resp = self.app.delete_json('/containers/{0}/consumers/'.format(
            self.container.id
        ), self.consumer_ref, expect_errors=True)
        self.consumer_repo.delete_entity_by_id.side_effect = None
        self.assertEqual(resp.status_int, 404)
        # Error response should have json content type
        self.assertEqual(resp.content_type, "application/json")

    def test_should_delete_consumers_on_container_delete(self):
        consumers = [self.consumer, self.consumer2]
        ret_val = (consumers, 0, 0, 1)
        self.consumer_repo.get_by_container_id.return_value = ret_val

        resp = self.app.delete(
            '/containers/{0}/'.format(self.container.id)
        )
        self.assertEqual(resp.status_int, 204)

        # Verify consumers were deleted
        calls = []
        for consumer in consumers:
            calls.append(mock.call(consumer.id, self.external_project_id))
        self.consumer_repo.delete_entity_by_id.assert_has_calls(
            calls, any_order=True
        )

    def test_should_pass_on_container_delete_with_missing_consumers(self):
        consumers = [self.consumer, self.consumer2]
        ret_val = (consumers, 0, 0, 1)
        self.consumer_repo.get_by_container_id.return_value = ret_val
        self.consumer_repo.delete_entity_by_id.side_effect = excep.NotFound

        resp = self.app.delete(
            '/containers/{0}/'.format(self.container.id)
        )
        self.assertEqual(resp.status_int, 204)

        # Verify consumers were deleted
        calls = []
        for consumer in consumers:
            calls.append(mock.call(consumer.id, self.external_project_id))
        self.consumer_repo.delete_entity_by_id.assert_has_calls(
            calls, any_order=True
        )


class WhenPerformingUnallowedOperationsOnConsumers(FunctionalTest):
    def setUp(self):
        super(
            WhenPerformingUnallowedOperationsOnConsumers, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.name = 'test container name'
        self.type = 'generic'
        self.secret_refs = [
            {
                'name': 'test secret 1',
                'secret_ref': '1231'
            },
            {
                'name': 'test secret 2',
                'secret_ref': '1232'
            },
            {
                'name': 'test secret 3',
                'secret_ref': '1233'
            }
        ]

        self.consumer_ref = {
            'name': 'test_consumer1',
            'URL': 'http://consumer/1'
        }
        self.external_project_id = 'keystoneid1234'
        self.project_internal_id = 'projectid1234'

        # Set up mocked project
        self.project = models.Project()
        self.project.id = self.project_internal_id
        self.project.external_id = self.external_project_id

        # Set up mocked project repo
        self.project_repo = mock.MagicMock()
        self.project_repo.get.return_value = self.project
        self.setup_project_repository_mock(self.project_repo)

        # Set up mocked container
        self.container = create_container(id_ref='id1')

        # Set up mocked container consumers
        self.consumer = create_consumer(self.container.id, id_ref='id2')
        self.consumer2 = create_consumer(self.container.id, id_ref='id3')

        self.consumer_ref = {
            'name': self.consumer.name,
            'URL': self.consumer.URL
        }

        # Set up container repo
        self.container_repo = mock.MagicMock()
        self.container_repo.get.return_value = self.container
        self.setup_container_repository_mock(self.container_repo)

        # Set up container consumer repo
        self.consumer_repo = mock.MagicMock()
        self.consumer_repo.get_by_values.return_value = self.consumer
        self.consumer_repo.delete_entity_by_id.return_value = None
        self.setup_container_consumer_repository_mock(self.consumer_repo)

        # Set up secret repo
        self.setup_secret_repository_mock()

    def test_should_not_allow_put_on_consumers(self):
        ret_val = ([self.consumer], 0, 0, 1)
        self.consumer_repo.get_by_container_id.return_value = ret_val

        resp = self.app.put_json(
            '/containers/{0}/consumers/'.format(self.container.id),
            self.consumer_ref,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_post_on_consumer_by_id(self):
        self.consumer_repo.get.return_value = self.consumer
        resp = self.app.post_json(
            '/containers/{0}/consumers/{1}/'.format(self.container.id,
                                                    self.consumer.id),
            self.consumer_ref,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_put_on_consumer_by_id(self):
        self.consumer_repo.get.return_value = self.consumer
        resp = self.app.put_json(
            '/containers/{0}/consumers/{1}/'.format(self.container.id,
                                                    self.consumer.id),
            self.consumer_ref,
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)

    def test_should_not_allow_delete_on_consumer_by_id(self):
        self.consumer_repo.get.return_value = self.consumer
        resp = self.app.delete(
            '/containers/{0}/consumers/{1}/'.format(self.container.id,
                                                    self.consumer.id),
            expect_errors=True
        )
        self.assertEqual(resp.status_int, 405)


class WhenGettingContainersListUsingResource(FunctionalTest):
    def setUp(self):
        super(
            WhenGettingContainersListUsingResource, self
        ).setUp()
        self.app = webtest.TestApp(app.build_wsgi_app(self.root))
        self.app.extra_environ = get_barbican_env(self.external_project_id)

    @property
    def root(self):
        self._init()

        class RootController(object):
            containers = controllers.containers.ContainersController()

        return RootController()

    def _init(self):
        self.project_id = 'project1234'
        self.external_project_id = 'keystoneid1234'

        self.num_containers = 10
        self.offset = 2
        self.limit = 2

        self.containers = [create_container(id_ref='id' + str(id_ref)) for
                           id_ref in moves.range(self.num_containers)]
        self.total = len(self.containers)

        self.container_repo = mock.MagicMock()
        self.container_repo.get_by_create_date.return_value = (self.containers,
                                                               self.offset,
                                                               self.limit,
                                                               self.total)
        self.setup_container_repository_mock(self.container_repo)
        self.setup_project_repository_mock()
        self.setup_secret_repository_mock()
        self.setup_container_consumer_repository_mock()

        self.params = {
            'offset': self.offset,
            'limit': self.limit,
        }

    def test_should_get_list_containers(self):
        resp = self.app.get(
            '/containers/',
            self.params
        )

        self.container_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True
        )

        self.assertTrue('previous' in resp.namespace)
        self.assertTrue('next' in resp.namespace)

        url_nav_next = self._create_url(self.external_project_id,
                                        self.offset + self.limit, self.limit)
        self.assertTrue(resp.body.count(url_nav_next) == 1)

        url_nav_prev = self._create_url(self.external_project_id,
                                        0, self.limit)
        self.assertTrue(resp.body.count(url_nav_prev) == 1)

        url_hrefs = self._create_url(self.external_project_id)
        self.assertTrue(resp.body.count(url_hrefs) ==
                        (self.num_containers + 2))

    def test_response_should_include_total(self):
        resp = self.app.get(
            '/containers/',
            self.params
        )
        self.assertIn('total', resp.namespace)
        self.assertEqual(resp.namespace['total'], self.total)

    def test_should_handle_no_containers(self):

        del self.containers[:]

        resp = self.app.get(
            '/containers/',
            self.params
        )

        self.container_repo.get_by_create_date.assert_called_once_with(
            self.external_project_id,
            offset_arg=u'{0}'.format(self.offset),
            limit_arg=u'{0}'.format(self.limit),
            suppress_exception=True
        )

        self.assertFalse('previous' in resp.namespace)
        self.assertFalse('next' in resp.namespace)

    def _create_url(self, external_project_id, offset_arg=None,
                    limit_arg=None):
        if limit_arg:
            offset = int(offset_arg)
            limit = int(limit_arg)
            return '/containers?limit={0}&offset={1}'.format(limit, offset)
        else:
            return '/containers'
